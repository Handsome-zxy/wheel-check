# tests/test_probe.py
import importlib.util, pathlib, unittest

PROBE = pathlib.Path(__file__).resolve().parents[1] / "skills" / "wheel-check" / "scripts" / "probe.py"

def load_probe():
    spec = importlib.util.spec_from_file_location("probe", PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class TestContract(unittest.TestCase):
    def test_module_exposes_exit_codes(self):
        p = load_probe()
        self.assertEqual((p.EXIT_OK, p.EXIT_DEGRADED, p.EXIT_BAD_ARGS), (0, 2, 3))

class TestValidateTerms(unittest.TestCase):
    def test_splits_and_strips_pipe_separated_terms(self):
        p = load_probe()
        self.assertEqual(p.validate_terms("test selection| affected tests "),
                         ["test selection", "affected tests"])

    def test_empty_string_raises(self):
        p = load_probe()
        with self.assertRaises(ValueError):
            p.validate_terms("")

    def test_only_separators_raises(self):
        p = load_probe()
        with self.assertRaises(ValueError):
            p.validate_terms("|  |")

class TestCliBadArgs(unittest.TestCase):
    def test_empty_terms_exits_3(self):
        p = load_probe()
        self.assertEqual(p.main(["search", "--terms", ""]), 3)

    def test_bad_source_exits_3(self):
        p = load_probe()
        self.assertEqual(p.main(["search", "--terms", "x", "--source", "nope"]), 3)

if __name__ == "__main__":
    unittest.main()
