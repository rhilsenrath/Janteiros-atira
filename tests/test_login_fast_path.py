"""Model-free login steps: unambiguous login form, necessary cookies, retry on temporary errors."""
import unittest
from agents.website import application_visible, login_actions, quick_access_action


def page(controls, text="Lieferantenportal"):
    return {"url": "https://p/login", "frames": [{"text": text, "controls": [
        {"target": f"f0:e{i}", **c} for i, c in enumerate(controls)]}]}


LOGIN = [{"tag": "input", "type": None, "label": "Benutzername"}, {"tag": "input", "type": "password", "label": "Passwort"},
         {"tag": "button", "type": None, "label": "Anmelden"}]


class FastPath(unittest.TestCase):
    def test_unambiguous_login_form_is_filled_in_one_go(self):
        actions = login_actions(page(LOGIN))
        self.assertEqual([(a.operation, a.target, a.secret) for a in actions],
                         [("fill", "f0:e0", "username"), ("fill", "f0:e1", "password"), ("click", "f0:e2", None)])

    def test_ambiguous_login_form_is_left_to_the_model(self):
        self.assertEqual(login_actions(page(LOGIN + [{"tag": "button", "type": "submit", "label": "Suchen"}])), [])
        self.assertEqual(login_actions(page(LOGIN[1:])), [])

    def test_necessary_cookies_and_retry_are_clicked(self):
        cookie = page([{"tag": "button", "label": "Notwendige Cookies akzeptieren"},
                       {"tag": "button", "label": "Alle akzeptieren"}])
        self.assertEqual(quick_access_action(cookie).target, "f0:e0")
        retry = page([{"tag": "a", "label": "Erneut versuchen"}], "Dienst vorübergehend nicht verfügbar")
        self.assertEqual(quick_access_action(retry).target, "f0:e0")
        self.assertIsNone(quick_access_action(page([{"tag": "a", "label": "Erneut versuchen"}])))

    def test_access_checks_are_never_operated_and_block_success(self):
        robot = page([{"tag": "input", "type": "checkbox", "label": "Ich bin kein Roboter"},
                      {"tag": "button", "label": "Weiter"}], "Zugangsprüfung")
        self.assertIsNone(quick_access_action(robot))
        self.assertFalse(application_visible(robot))

    def test_application_visible_only_without_login_consent_or_error(self):
        self.assertTrue(application_visible(page([{"tag": "a", "label": "Dokumente"}])))
        self.assertFalse(application_visible(page(LOGIN)))
        self.assertFalse(application_visible(page([{"tag": "button", "label": "Notwendige Cookies akzeptieren"}])))


if __name__ == "__main__":
    unittest.main()
