import AppShell from "./AppShell";
import SetupPage from "./features/auth/SetupPage";
import SignInPage from "./features/auth/SignInPage";
import AskPage from "./features/ask/AskPage";
import ActivityPage from "./features/activity/ActivityPage";
import AnalyticsPage from "./features/analytics/AnalyticsPage";
import UserAccessPage from "./features/settings/UserAccessPage";
import RetentionSettings from "./features/settings/RetentionSettings";
import AIProviderSettings from "./features/settings/AIProviderSettings";
import BackupRestore from "./features/settings/BackupRestore";
import SourceListPage from "./features/sources/SourceListPage";
import { navigate, usePath } from "./router";

function App() {
  const path = usePath();
  if (path === "/setup") return <SetupPage />;
  if (path === "/signin") return <SignInPage />;
  if (path === "/ask")
    return (
      <AppShell>
        <AskPage />
      </AppShell>
    );
  if (path === "/users")
    return (
      <AppShell>
        <UserAccessPage />
      </AppShell>
    );
  if (path === "/settings/retention")
    return (
      <AppShell>
        <RetentionSettings />
      </AppShell>
    );
  if (path === "/settings/ai-provider")
    return (
      <AppShell>
        <AIProviderSettings />
      </AppShell>
    );
  if (path === "/settings/backup")
    return (
      <AppShell>
        <BackupRestore />
      </AppShell>
    );
  if (path === "/activity")
    return (
      <AppShell>
        <ActivityPage />
      </AppShell>
    );
  if (path === "/analytics")
    return (
      <AppShell>
        <AnalyticsPage />
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
