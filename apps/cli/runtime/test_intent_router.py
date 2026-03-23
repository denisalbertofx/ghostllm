import unittest
from intent_router import IntentRouter, Intent

class TestIntentRouter(unittest.TestCase):
    def setUp(self):
        self.router = IntentRouter()

    def test_ask_intent(self):
        intent = self.router.route("Hola, ¿cómo estás?")
        self.assertEqual(intent.task_type, "ask")
        self.assertFalse(intent.requires_tools)

    def test_architect_intent(self):
        intent = self.router.route("Diseña una pantalla de login para mi app")
        self.assertEqual(intent.task_type, "architect")
        
        intent = self.router.route("Dame la arquitectura de este proyecto")
        self.assertEqual(intent.task_type, "architect")

    def test_debug_intent(self):
        intent = self.router.route("Tengo un error en el servidor, no arranca")
        self.assertEqual(intent.task_type, "debug")
        self.assertTrue(intent.requires_tools)
        self.assertTrue(intent.requires_file_scope)

    def test_code_intent(self):
        intent = self.router.route("Escribe una función para sumar dos números")
        self.assertEqual(intent.task_type, "code")
        self.assertTrue(intent.requires_tools)

    def test_fix_intent(self):
        intent = self.router.route("Arregla el bug en el login")
        self.assertEqual(intent.task_type, "fix")
        self.assertTrue(intent.requires_tools)

    def test_review_intent(self):
        intent = self.router.route("Revisa mi código en assistant.py")
        self.assertEqual(intent.task_type, "review")
        self.assertTrue(intent.requires_tools)
        self.assertTrue(intent.requires_file_scope)

    def test_research_intent(self):
        intent = self.router.route("Busca donde se define la clase CodexAssistant")
        self.assertEqual(intent.task_type, "research")
        self.assertTrue(intent.requires_tools)

    def test_slash_command(self):
        intent = self.router.route("/plan dame la estructura")
        self.assertEqual(intent.mode, "Plan")
        self.assertTrue(intent.is_slash_command)

    def test_bypass_intent(self):
        intent = self.router.route("conteo de lineas en main.py")
        self.assertEqual(intent.mode, "Analyze")
        self.assertEqual(intent.task_type, "research")

if __name__ == "__main__":
    unittest.main()
