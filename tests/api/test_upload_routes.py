"""Task 9b: multipart upload over the API — validation, reasons, denials."""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.datastructures import UploadFile

from apps.api import main as api
from apps.api.routes import uploads as upload_routes
from foundation.config import Settings
from foundation.events import EventWriter
from foundation.unit_of_work import UnitOfWork
from identity_access.setup import create_first_owner

OWNER = {"email": "owner@example.com", "password": "owner-password-1"}
MEMBER = {"email": "member@example.com", "password": "member-password-1"}
VALID_PDF = b"%PDF-1.7 fake pdf body"


@pytest.fixture(scope="module", autouse=True)
def _migrated():
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    command.upgrade(Config(str(root / "alembic.ini")), "head")


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    async def create() -> dict:
        engine = create_async_engine(Settings.load().database_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE source_versions, sources, collections, invitations, "
                    "sessions, users, workspaces CASCADE"
                )
            )
            await connection.execute(text("TRUNCATE audit_events"))
        uow = UnitOfWork(engine)
        try:
            owner = await create_first_owner(uow, EventWriter(), **OWNER)
        finally:
            await engine.dispose()
        return {"workspace_id": owner.workspace_id, "owner_id": owner.user_id}

    result = asyncio.run(create())
    monkeypatch.setattr(upload_routes, "_store", lambda: upload_routes.ObjectStore(tmp_path / "objects"))
    return result


def _sign_in(client: TestClient, person: dict) -> None:
    response = client.post("/api/session", json=person)
    assert response.status_code == 200, response.text


def _invite_and_accept(client: TestClient, person: dict, role: str) -> None:
    created = client.post(
        "/api/users/invitations",
        json={"email": person["email"], "role": role, "collections": []},
    )
    assert created.status_code == 201
    TestClient(api.app).post(
        "/api/invitations/accept",
        json={"token": created.json()["token"], "password": person["password"]},
    )


def _collection_id(client: TestClient) -> str:
    return client.post("/api/collections", json={"name": "Policies"}).json()["collection_id"]


def _upload(client: TestClient, data: bytes, filename: str, collection_id: str,
            name: str = "Handbook"):
    return client.post(
        "/api/uploads",
        files={"file": (filename, data)},
        data={"collection_id": collection_id, "name": name},
    )


def test_owner_uploads_pdf_and_duplicate_warns(workspace, tmp_path) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        collection_id = _collection_id(client)
        first = _upload(client, VALID_PDF, "handbook.pdf", collection_id)
        second = _upload(client, VALID_PDF, "handbook-copy.pdf", collection_id)

    assert first.status_code == 201, first.text
    body = first.json()
    assert body["source_id"] and body["source_version_id"]
    assert body["media_type"] == "application/pdf"
    assert body["duplicate"] is False
    assert second.json()["duplicate"] is True

    objects = list((tmp_path / "objects" / "objects").iterdir())
    assert len(objects) == 1


def test_over_limit_is_413(workspace, monkeypatch) -> None:
    monkeypatch.setattr(upload_routes, "_limit_bytes", lambda: 4)
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        collection_id = _collection_id(client)
        response = _upload(client, b"%PDF- way too big", "big.pdf", collection_id)

    assert response.status_code == 413
    assert "50 MB" in response.json()["detail"]


def test_rejection_reasons_are_actionable(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        collection_id = _collection_id(client)
        corrupt = _upload(client, b"garbage", "broken.pdf", collection_id)
        encrypted = _upload(
            client, b"%PDF-1.7 /Encrypt 4 0 R x", "locked.pdf", collection_id
        )
        unsupported = _upload(client, b"MZ", "tool.exe", collection_id)

    assert corrupt.status_code == 400
    assert "not a valid PDF" in corrupt.json()["detail"]
    assert encrypted.status_code == 400
    assert "remove protection" in encrypted.json()["detail"]
    assert unsupported.status_code == 400
    assert "unsupported file type" in unsupported.json()["detail"]


def test_member_upload_and_unknown_collection_denied(workspace) -> None:
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        _invite_and_accept(client, MEMBER, "member")
        _sign_in(client, MEMBER)
        member_upload = _upload(client, VALID_PDF, "h.pdf", str(uuid.uuid4()))
        _sign_in(client, OWNER)
        foreign = _upload(client, VALID_PDF, "h.pdf", str(uuid.uuid4()))

    assert member_upload.status_code == 403
    assert foreign.status_code == 404


def test_interrupted_stream_is_400(workspace, monkeypatch) -> None:
    async def broken_read(self, size=-1):
        raise OSError("broken pipe")

    monkeypatch.setattr(UploadFile, "read", broken_read)
    with TestClient(api.app) as client:
        _sign_in(client, OWNER)
        collection_id = _collection_id(client)
        response = _upload(client, VALID_PDF, "handbook.pdf", collection_id)

    assert response.status_code == 400
    assert "interrupted" in response.json()["detail"]
