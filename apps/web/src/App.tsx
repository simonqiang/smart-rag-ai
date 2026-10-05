import { useEffect, useState } from "react";

import SetupPage from "./features/auth/SetupPage";
import SignInPage from "./features/auth/SignInPage";
import UserAccessPage from "./features/settings/UserAccessPage";

function usePath(): string {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const update = () => setPath(window.location.pathname);
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  return path;
}

function navigate(path: string) {
  window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

function App() {
  const path = usePath();
  if (path === "/setup") return <SetupPage />;
  if (path === "/signin") return <SignInPage />;
  if (path === "/users") return <UserAccessPage />;
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
