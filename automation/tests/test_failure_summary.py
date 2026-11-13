import subprocess
import unittest

from automation.run_sync import failure_summary


class FailureSummaryTests(unittest.TestCase):
    def test_server_failure_identifies_fetch_without_disclosing_private_context(self):
        private = ["https://gitee.com/private-owner/private-repo.git", "credential-value", "private-branch", "a" * 40]
        error = subprocess.CalledProcessError(128, ["git", "fetch", private[0]], stderr="fatal: unable to access " + " ".join(private) + ": The requested URL returned error: 502")
        summary = failure_summary(error)
        self.assertIn("operation=fetch", summary)
        self.assertIn("exit=128", summary)
        self.assertIn("category=server", summary)
        for value in private:
            self.assertNotIn(value, summary)

    def test_connection_failure_is_classified(self):
        error = subprocess.CalledProcessError(128, ["git", "ls-remote", "private-url"], stderr="fatal: Failed to connect to server: Connection timed out")
        self.assertIn("operation=ls-remote", failure_summary(error))
        self.assertIn("category=network", failure_summary(error))

    def test_authentication_failure_is_distinct(self):
        error = subprocess.CalledProcessError(128, ["git", "push", "private-url"], stderr="fatal: Authentication failed for private-url")
        self.assertIn("category=authentication", failure_summary(error))

    def test_unknown_failure_never_echoes_raw_command_or_stderr(self):
        error = subprocess.CalledProcessError(1, ["private-command", "private-repository"], stderr="private-token")
        summary = failure_summary(error)
        self.assertIn("operation=unknown", summary)
        self.assertNotIn("private-", summary)
        self.assertNotIn("sensitive-message", failure_summary(RuntimeError("sensitive-message")))


if __name__ == "__main__":
    unittest.main()
