import os
import tempfile
import unittest
from dataclasses import replace
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
        self.user_root = Path(self.tmp.name) / "config" / "skills"
        self.builtin_root.mkdir()
        self.user_root.mkdir(parents=True)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.user_root.parent.parent)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_skill(self, root, name, *, description="A test skill.", body="Do something useful.", allowed_tools=None, triggers=None, model=None, metadata=None, compatibility=None, frontmatter_name=None):
        skill_dir = Path(root) / name
        skill_dir.mkdir(parents=True)
        lines = ["---"]
        lines.append(f"name: {frontmatter_name or name}")
        lines.append(f"description: {description}")
        if allowed_tools is not None:
            lines.append(f"allowed-tools: {allowed_tools}")
        metadata_lines = dict(metadata or {})
        if triggers is not None:
            metadata_lines["aios-triggers"] = triggers
        if model is not None:
            metadata_lines["aios-model"] = model
        if metadata_lines:
            lines.append("metadata:")
            for key, value in metadata_lines.items():
                lines.append(f"  {key}: {value}")
        if compatibility is not None:
            lines.append(f"compatibility: {compatibility}")
        lines.extend(["---", body])
        (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
        return skill_dir

    def write_skill_text(self, root, name, text):
        skill_dir = Path(root) / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
        return skill_dir

    def load_catalog(self, *, builtin_root=None, **kwargs):
        skills = load_skills_module()
        with patch.object(skills, "BUILTIN_SKILLS_ROOT", builtin_root or self.builtin_root), patch.object(skills, "USER_SKILLS_ROOT", self.user_root):
            return skills.load_skills(**kwargs)

    def load_real_builtin_catalog(self, **kwargs):
        skills_root = Path(__file__).resolve().parents[1] / "apps" / "skills"
        skills = load_skills_module()
        with patch.object(skills, "BUILTIN_SKILLS_ROOT", skills_root), patch.object(skills, "USER_SKILLS_ROOT", self.user_root):
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

    def test_nested_metadata_parses_triggers_and_model(self):
        self.write_skill(self.builtin_root, "application-builder", description="calculator builder", body="builds apps", metadata={"aios-triggers": "build an app, need a calculator", "aios-model": "remote-preferred"})
        catalog = self.load_catalog()
        skill = catalog[0]
        self.assertEqual(skill.triggers, ("build an app", "need a calculator"))
        self.assertEqual(skill.model, "remote-preferred")

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

    def test_unterminated_double_quote_is_rejected_with_warning(self):
        self.write_skill_text(
            self.builtin_root,
            "quote-skill",
            '\n'.join([
                "---",
                'name: quote-skill',
                'description: "unterminated',
                "---",
                "body",
            ]),
        )
        catalog, warnings = self.load_catalog(include_warnings=True)
        self.assertEqual(catalog, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("quote-skill", warnings[0])

    def test_unterminated_single_quote_is_rejected_with_warning(self):
        self.write_skill_text(
            self.builtin_root,
            "single-quote-skill",
            '\n'.join([
                "---",
                "name: single-quote-skill",
                "description: 'unterminated",
                "---",
                "body",
            ]),
        )
        catalog, warnings = self.load_catalog(include_warnings=True)
        self.assertEqual(catalog, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("single-quote-skill", warnings[0])

    def test_malformed_metadata_indentation_is_rejected_with_warning(self):
        self.write_skill_text(
            self.builtin_root,
            "bad-metadata",
            '\n'.join([
                "---",
                "name: bad-metadata",
                "description: bad metadata",
                "metadata:",
                "  aios-triggers: build an app",
                "    aios-model: remote-preferred",
                "---",
                "body",
            ]),
        )
        catalog, warnings = self.load_catalog(include_warnings=True)
        self.assertEqual(catalog, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("bad-metadata", warnings[0])

    def test_large_skill_is_skipped_with_safe_warning(self):
        self.write_skill(
            self.builtin_root,
            "huge-skill",
            description="huge",
            body="x" * (49 * 1024),
        )
        catalog, warnings = self.load_catalog(include_warnings=True)
        self.assertEqual(catalog, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("huge-skill", warnings[0])
        self.assertNotIn(str(self.tmp.name), warnings[0])

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

    def test_real_application_builder_negated_phrases_do_not_activate(self):
        catalog = self.load_real_builtin_catalog()
        prompts = (
            "I don't need a calculator",
            "I do not need a calculator",
            "never build an app for me",
            "no need a calculator",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                activated = load_skills_module().initial_skills(catalog, prompt)
                self.assertEqual(activated, [])

    def test_real_application_builder_without_making_an_app_stays_negated_when_matching(self):
        catalog = self.load_real_builtin_catalog()
        application_builder = next(skill for skill in catalog if skill.name == "application-builder")
        augmented = replace(application_builder, triggers=application_builder.triggers + ("making an app",))
        activated = load_skills_module().initial_skills([augmented], "without making an app")
        self.assertEqual(activated, [])

    def test_real_application_builder_positive_intent_still_activates(self):
        catalog = self.load_real_builtin_catalog()
        prompts = (
            "I need a calculator",
            "please build an app",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                activated = load_skills_module().initial_skills(catalog, prompt)
                self.assertEqual([skill.name for skill in activated], ["application-builder"])

    def test_real_application_builder_explicit_slash_activation_overrides_negation(self):
        catalog = self.load_real_builtin_catalog()
        activated = load_skills_module().initial_skills(catalog, "/application-builder never build an app for me")
        self.assertEqual([skill.name for skill in activated], ["application-builder"])

    def test_real_application_builder_skill_loads_cleanly(self):
        catalog, warnings = self.load_real_builtin_catalog(include_warnings=True)
        self.assertEqual(warnings, [])
        skill = next(skill for skill in catalog if skill.name == "application-builder")
        self.assertEqual(skill.allowed_tools, ("application",))
        self.assertEqual(skill.model, "remote-preferred")
        self.assertIn("build an app", skill.triggers)
        self.assertIn("need a calculator", skill.triggers)
        self.assertNotIn("Compatibility:", skill.instructions)
        self.assertIn("Search first for an existing cached app", skill.instructions)


if __name__ == "__main__":
    unittest.main()
