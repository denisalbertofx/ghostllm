import unittest
import os
import json
import shutil
import tempfile
from apps.cli.runtime.verification import VerificationManager

class TestVerificationManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.manager = VerificationManager(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_detect_node_with_build(self):
        # Setup package.json with build script
        pkg = {"scripts": {"build": "echo 'building'"}}
        with open(os.path.join(self.test_dir, "package.json"), "w") as f:
            json.dump(pkg, f)
        
        checks = self.manager.get_applicable_checks()
        names = [c["name"] for c in checks]
        self.assertIn("Build", names)
        self.assertEqual(checks[0]["command"], "npm run build")

    def test_detect_node_ts_fallback(self):
        # Setup package.json WITHOUT build script but WITH tsconfig
        with open(os.path.join(self.test_dir, "package.json"), "w") as f:
            json.dump({}, f)
        with open(os.path.join(self.test_dir, "tsconfig.json"), "w") as f:
            f.write("{}")
        
        checks = self.manager.get_applicable_checks()
        self.assertEqual(checks[0]["name"], "TypeCheck")
        self.assertEqual(checks[0]["command"], "npx tsc --noEmit")

    def test_detect_python_pytest(self):
        # Setup pyproject.toml and tests folder
        with open(os.path.join(self.test_dir, "pyproject.toml"), "w") as f:
            f.write("")
        os.makedirs(os.path.join(self.test_dir, "tests"))
        
        # Mock uv not present
        checks = self.manager.get_applicable_checks()
        # It will fallback to pytest or unittest depending on environment, 
        # but let's check one of the python ones is present
        self.assertTrue(any("Python Tests" in c["name"] for c in checks))

    def test_verify_change_output_structure(self):
        # Setup simple echo test
        pkg = {"scripts": {"build": "echo 'success'"}}
        with open(os.path.join(self.test_dir, "package.json"), "w") as f:
            json.dump(pkg, f)
            
        res = self.manager.verify_change()
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(res["checks"]), 1)
        self.assertEqual(res["checks"][0]["name"], "Build")
        self.assertEqual(res["checks"][0]["status"], "passed")

if __name__ == "__main__":
    unittest.main()
