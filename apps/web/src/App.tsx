import AppShell from "./AppShell";
import SetupPage from "./features/auth/SetupPage";
import SignInPage from "./features/auth/SignInPage";
import UserAccessPage from "./features/settings/UserAccessPage";
import SourceListPage from "./features/sources/SourceListPage";
import { navigate, usePath } from "./router";

function App() {
  const path = usePath();
  if (path === "/setup") return <SetupPage />;
  if (path === "/signin") return <SignInPage />;
  if (path === "/users")
    return (
      <AppShell>
        <UserAccessPage />
      </AppShell>
    );
  if (path === "/sources")
    return (
      <AppShell>
        <SourceListPage />
      </AppShell>
    );
  return (
    <main>
      <h1>Smart RAG AI</h1>
      <p>Local-first knowledge assistant with trustworthy citations.</p>
      <nav aria-label="primary">
        <a
          href="/setup"
          onClick={(event) => {
            event.preventDefault();
            navigate("/setup");
          }}
        >
          Set up your workspace
        </a>
        {" · "}
        <a
          href="/signin"
          onClick={(event) => {
            event.preventDefault();
            navigate("/signin");
          }}
        >
          Sign in
        </a>
      </nav>
    </main>
  );
}

export default App;
