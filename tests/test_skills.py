import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def load_skills_module():
    try:
        from aios import skills
    except ModuleNotFoundError as exc:
        raise AssertionError(f"aios.skills missing: {exc}") from exc
    return skills


class SkillsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.builtin_root = Path(self.tmp.name) / "builtin"
        self.user_root = Path(self.tmp.name) / "user"
        self.builtin_root.mkdir()
        self.user_root.mkdir(parents=True)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.user_root.parent)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_skill(self, root, name, *, description="A test skill.", body="Do something useful.", allowed_tools=None, triggers=None, model=None, frontmatter_name=None):
        skill_dir = Path(root) / name
        skill_dir.mkdir(parents=True)
        lines = ["---"]
        lines.append(f"name: {frontmatter_name or name}")
        lines.append(f"description: {description}")
        if allowed_tools is not None:
            lines.append(f"allowed-tools: {allowed_tools}")
        if triggers is not None:
            lines.append(f"metadata.aios-triggers: {triggers}")
        if model is not None:
            lines.append(f"metadata.aios-model: {model}")
        lines.extend(["---", body])
        (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
        return skill_dir

    def load_catalog(self, **kwargs):
        skills = load_skills_module()
        with patch.object(skills, "BUILTIN_SKILLS_ROOT", self.builtin_root), patch.object(skills, "USER_SKILLS_ROOT", self.user_root):
            return skills.load_skills(**kwargs)

    def test_module_can_be_imported(self):
        skills = load_skills_module()
        self.assertTrue(hasattr(skills, "load_skills"))

    def test_user_override_precedence(self):
        self.write_skill(self.builtin_root, "application-builder", description="built-in", body="built in body")
        self.write_skill(self.user_root, "application-builder", description="user override", body="user body")
        catalog = self.load_catalog()
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0].description, "user override")
        self.assertEqual(catalog[0].instructions, "user body")

    def test_trigger_activates_application_builder_for_need_a_calculator(self):
        self.write_skill(self.builtin_root, "application-builder", description="calculator builder", body="builds apps", triggers="build an app, make an app, need an app, need a calculator, calculator, timer, converter, tracker, dashboard, game", model="remote-preferred")
        catalog = self.load_catalog()
        activated = load_skills_module().initial_skills(catalog, "I need a calculator")
        self.assertEqual([skill.name for skill in activated], ["application-builder"])

    def test_explicit_slash_activation_for_notes(self):
        self.write_skill(self.builtin_root, "notes", description="notes skill", body="take notes")
        catalog = self.load_catalog()
        activated = load_skills_module().initial_skills(catalog, "/notes")
        self.assertEqual([skill.name for skill in activated], ["notes"])

    def test_initial_skills_is_capped_at_three(self):
        for name in ("alpha", "beta", "delta", "gamma"):
            self.write_skill(self.builtin_root, name, description=name, body=name, triggers="need a widget")
        catalog = self.load_catalog()
        activated = load_skills_module().initial_skills(catalog, "I need a widget")
        self.assertEqual(len(activated), 3)
        self.assertEqual([skill.name for skill in activated], ["alpha", "beta", "delta"])

    def test_catalog_prompt_hides_instructions(self):
        self.write_skill(self.builtin_root, "application-builder", description="catalog description", body="secret instructions")
        catalog = self.load_catalog()
        prompt = load_skills_module().catalog_prompt(catalog)
        self.assertIn("application-builder", prompt)
        self.assertIn("catalog description", prompt)
        self.assertNotIn("secret instructions", prompt)

    def test_invalid_skill_is_skipped_with_safe_warning(self):
        self.write_skill(self.builtin_root, "bad-skill", description="bad", body="body", frontmatter_name="different-name")
        catalog, warnings = self.load_catalog(include_warnings=True)
        self.assertEqual(catalog, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("bad-skill", warnings[0])
        self.assertNotIn(str(self.tmp.name), warnings[0])

    def test_invalid_model_is_skipped(self):
        self.write_skill(self.builtin_root, "model-skill", description="model", body="body", model="space-warp")
        catalog, warnings = self.load_catalog(include_warnings=True)
        self.assertEqual(catalog, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("model-skill", warnings[0])

    def test_catalog_limit_is_64(self):
        for index in range(65):
            self.write_skill(self.builtin_root, f"skill{index}", description=f"skill {index}", body=f"body {index}")
        catalog = self.load_catalog()
        self.assertEqual(len(catalog), 64)

    def test_application_does_not_match_application_builder_by_substring(self):
        self.write_skill(self.builtin_root, "application-builder", description="calculator builder", body="builds apps", triggers="build an app, make an app, need an app, need a calculator, calculator, timer, converter, tracker, dashboard, game")
        catalog = self.load_catalog()
        activated = load_skills_module().initial_skills(catalog, "application")
        self.assertEqual(activated, [])


if __name__ == "__main__":
    unittest.main()
