import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from aios import agent
from aios.applications import APPLICATION_TOOL
from aios.browser import Browser, TOOL as BROWSER_TOOL, web_url
from aios.skills import Skill


def stream(delta, finish="stop"):
    events = [{"choices": [{"index": 0, "delta": value}]} for value in delta]
    events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    payload = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return io.BytesIO((payload + "data: [DONE]\n\n").encode())


def clone(value):
    return json.loads(json.dumps(value))


def host_tool(name, description=None, parameters=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or f"{name} description",
            "parameters": parameters or {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


class BrowserAgentTests(unittest.TestCase):
    def test_web_urls_and_lazy_start(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "******example.com", "--no-sandbox", None):
            with self.assertRaises(ValueError):
                web_url(url)
        self.assertEqual(web_url("http://localhost:8000/?q=test#section"), "http://localhost:8000/?q=test#section")
        browser = Browser()
        with self.assertRaises(ValueError):
            browser.act({"action": "snapshot"})
        self.assertIsNone(browser.driver)
        self.assertEqual(browser.act({"action": "close"}), {"closed": True})

    @patch("aios.agent.core.load_config", return_value={"mode": "remote", "model": "test-model"})
    @patch("aios.agent.toolhost.list_tools", return_value={"tools": [clone(BROWSER_TOOL)], "warnings": []})
    def test_fragmented_generic_browser_call_then_grounded_response(self, _list_tools, _load_config):
        session = agent.AgentSession([{"role": "user", "content": "Open example.com"}], "private/tools.sock", catalog=[])
        bodies = []

        def request(route, body):
            bodies.append(clone(body))
            if len(bodies) == 1:
                return stream([
                    {"tool_calls": [{"index": 0, "id": "call_", "function": {"name": "brow", "arguments": "{\"action\":\"op"}}]},
                    {"tool_calls": [{"index": 0, "id": "1", "function": {"name": "ser", "arguments": "en\",\"url\":\"https://example.com\"}"}}]},
                ], "tool_calls")
            return stream([{"content": "The page is open."}])

        with patch("aios.agent.core.request", side_effect=request), patch(
            "aios.agent.toolhost.call",
            return_value={"title": "Example", "untrusted_page_content": True},
        ) as call:
            events = list(agent.openai_chat(session))

        call.assert_called_once_with("private/tools.sock", "browser", {"action": "open", "url": "https://example.com"})
        self.assertEqual(bodies[0]["model"], "test-model")
        self.assertIn("untrusted", bodies[0]["messages"][0]["content"])
        self.assertEqual(bodies[1]["messages"][-1]["role"], "tool")
        self.assertEqual(bodies[1]["messages"][-1]["tool_call_id"], "call_1")
        self.assertEqual(events[0], {"type": "progress", "text": "Browser · open"})
        self.assertEqual(events[-1], {"type": "token", "text": "The page is open."})

    @patch("aios.agent.core.load_config", return_value={"mode": "local"})
    @patch("aios.agent.toolhost.list_tools", return_value={"tools": [clone(BROWSER_TOOL)], "warnings": []})
    def test_incomplete_call_never_dispatches(self, _list_tools, _load_config):
        session = agent.AgentSession([{"role": "user", "content": "Hi"}], "tools.sock", catalog=[])
        with patch(
            "aios.agent.core.request",
            return_value=stream([{"tool_calls": [{"index": 0, "id": "x", "function": {"name": "browser", "arguments": "{}"}}]}], "length"),
        ), patch("aios.agent.toolhost.call") as call:
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                list(agent.openai_chat(session))
        call.assert_not_called()

    @patch("aios.agent.core.load_config", return_value={"mode": "local"})
    @patch("aios.agent.toolhost.list_tools", return_value={"tools": [clone(BROWSER_TOOL)], "warnings": []})
    def test_plain_json_like_prose_never_dispatches(self, _list_tools, _load_config):
        session = agent.AgentSession([{"role": "user", "content": "Hi"}], "tools.sock", catalog=[])
        with patch(
            "aios.agent.core.request",
            return_value=stream([{"content": "{\"action\":\"open\",\"url\":\"https://example.com\"}"}]),
        ), patch("aios.agent.toolhost.call") as call:
            events = list(agent.openai_chat(session))
        self.assertEqual(events, [{"type": "token", "text": "{\"action\":\"open\",\"url\":\"https://example.com\"}"}])
        call.assert_not_called()

    @patch("aios.agent.core.load_config", return_value={"mode": "local"})
    @patch("aios.agent.toolhost.list_tools", return_value={"tools": [clone(BROWSER_TOOL)], "warnings": []})
    def test_agent_tool_loop_is_bounded_to_eight_rounds(self, _list_tools, _load_config):
        session = agent.AgentSession([{"role": "user", "content": "Browse"}], "tools.sock", catalog=[])

        def request(route, body):
            self.assertEqual(body["model"], "local")
            return stream([
                {"tool_calls": [{"index": 0, "id": "tool", "function": {"name": "browser", "arguments": "{\"action\":\"snapshot\"}"}}]}
            ], "tool_calls")

        with patch("aios.agent.core.request", side_effect=request), patch(
            "aios.agent.toolhost.call",
            return_value={"text": "page"},
        ) as call:
            with self.assertRaisesRegex(RuntimeError, "Agent tool limit reached"):
                list(agent.openai_chat(session))
        self.assertEqual(call.call_count, 8)

    @patch("aios.agent.toolhost.list_tools")
    def test_application_builder_trigger_filters_tools_and_marks_remote_preference(self, list_tools):
        list_tools.return_value = {
            "tools": [clone(BROWSER_TOOL), clone(APPLICATION_TOOL), host_tool("mcp_notes_lookup")],
            "warnings": [],
        }
        catalog = [
            Skill(
                name="application-builder",
                description="Build local applications.",
                instructions="Search the cache before creating a new app.",
                allowed_tools=("application",),
                triggers=("calculator", "build an app"),
                model="remote-preferred",
            ),
            Skill(
                name="notes",
                description="Take structured notes.",
                instructions="Never show this inactive body.",
                allowed_tools=(),
                triggers=("notes",),
            ),
        ]

        session = agent.AgentSession([{"role": "user", "content": "I need a calculator"}], "tools.sock", catalog=catalog)

        self.assertTrue(session.remote_preferred)
        self.assertEqual([tool["function"]["name"] for tool in session.tools()], ["activate_skill", "application"])
        prompt = session.system_prompt()
        self.assertIn("application-builder: Build local applications.", prompt)
        self.assertIn("notes: Take structured notes.", prompt)
        self.assertIn("Activated skill: application-builder", prompt)
        self.assertIn("Search the cache before creating a new app.", prompt)
        self.assertNotIn("Never show this inactive body.", prompt)

    @patch("aios.agent.toolhost.list_tools")
    def test_explicit_notes_and_model_activation_recompute_tools(self, list_tools):
        list_tools.return_value = {"tools": [clone(BROWSER_TOOL), clone(APPLICATION_TOOL)], "warnings": []}
        catalog = [
            Skill(
                name="notes",
                description="Take notes.",
                instructions="Summarize and organize the notes.",
                allowed_tools=(),
                triggers=("notes",),
            ),
            Skill(
                name="application-builder",
                description="Build apps.",
                instructions="Use only application tools and search first.",
                allowed_tools=("application",),
                triggers=("calculator",),
                model="remote-preferred",
            ),
        ]

        session = agent.AgentSession([{"role": "user", "content": "/notes sort these"}], "tools.sock", catalog=catalog)

        self.assertEqual(list(session.active), ["notes"])
        self.assertEqual([tool["function"]["name"] for tool in session.tools()], ["activate_skill", "browser", "application"])
        self.assertIn("Summarize and organize the notes.", session.system_prompt())
        self.assertNotIn("Use only application tools and search first.", session.system_prompt())

        result = session.dispatch("activate_skill", {"name": "application-builder"})

        self.assertEqual(result, {"activated": "application-builder", "instructions": "Use only application tools and search first."})
        self.assertTrue(session.remote_preferred)
        self.assertEqual([tool["function"]["name"] for tool in session.tools()], ["activate_skill", "application"])
        prompt = session.system_prompt()
        self.assertIn("Activated skill: notes", prompt)
        self.assertIn("Activated skill: application-builder", prompt)
        self.assertIn("Use only application tools and search first.", prompt)

    @patch("aios.agent.toolhost.list_tools", return_value={"tools": [clone(BROWSER_TOOL)], "warnings": []})
    def test_unknown_fourth_skill_rejection_and_idempotence(self, _list_tools):
        catalog = [
            Skill(name="one", description="one", instructions="first", allowed_tools=(), triggers=("one",)),
            Skill(name="two", description="two", instructions="second", allowed_tools=(), triggers=("two",)),
            Skill(name="three", description="three", instructions="third", allowed_tools=(), triggers=("three",)),
            Skill(name="four", description="four", instructions="fourth", allowed_tools=(), triggers=("four",)),
        ]
        session = agent.AgentSession([{"role": "user", "content": "/one start"}], "tools.sock", catalog=catalog)
        session.tools()

        self.assertEqual(session.dispatch("activate_skill", {"name": "two"})["activated"], "two")
        self.assertEqual(session.dispatch("activate_skill", {"name": "three"})["activated"], "three")
        self.assertEqual(session.dispatch("activate_skill", {"name": "two"})["instructions"], "second")
        with self.assertRaisesRegex(ValueError, "installed"):
            session.dispatch("activate_skill", {"name": "missing"})
        with self.assertRaisesRegex(ValueError, "Too many"):
            session.dispatch("activate_skill", {"name": "four"})

    @patch("aios.agent.toolhost.list_tools")
    def test_dispatch_rejects_unadvertised_tool_even_if_host_would_accept(self, list_tools):
        list_tools.return_value = {"tools": [clone(BROWSER_TOOL), clone(APPLICATION_TOOL)], "warnings": []}
        catalog = [
            Skill(
                name="application-builder",
                description="Build apps.",
                instructions="Apps only.",
                allowed_tools=("application",),
                triggers=("calculator",),
            )
        ]
        session = agent.AgentSession([{"role": "user", "content": "calculator"}], "tools.sock", catalog=catalog)
        session.tools()

        with patch("aios.agent.toolhost.call", return_value={"ok": True}) as call:
            with self.assertRaisesRegex(ValueError, "unavailable"):
                session.dispatch("browser", {"action": "snapshot"})
            self.assertEqual(session.dispatch("application", {"action": "search", "query": "calc"}), {"ok": True})
        self.assertEqual(call.call_count, 1)

    @patch("aios.agent.toolhost.list_tools", return_value={"tools": [clone(APPLICATION_TOOL)], "warnings": []})
    def test_dispatch_enforces_json_and_size_bounds(self, _list_tools):
        session = agent.AgentSession([{"role": "user", "content": "hello"}], "tools.sock", catalog=[])
        session.tools()

        with patch("aios.agent.toolhost.call", return_value={"bad": {1, 2, 3}}):
            with self.assertRaisesRegex(RuntimeError, "invalid response"):
                session.dispatch("application", {"action": "search", "query": "calc"})

        with patch("aios.agent.toolhost.call", return_value={"blob": "x" * (70 * 1024)}):
            with self.assertRaisesRegex(RuntimeError, "too much data"):
                session.dispatch("application", {"action": "search", "query": "calc"})

    def test_malformed_toolhost_catalogs_are_rejected(self):
        bad_cases = [
            [],
            {"warnings": []},
            {"tools": "bad", "warnings": []},
            {"tools": [clone(BROWSER_TOOL)], "warnings": "bad"},
            {"tools": [host_tool("dup"), host_tool("dup")], "warnings": []},
            {"tools": [host_tool(f"tool-{index}") for index in range(257)], "warnings": []},
            {"tools": [host_tool("ok")], "warnings": [123]},
            {"tools": [host_tool("ok")], "warnings": ["x" * 301]},
            {"tools": [host_tool("ok")], "warnings": ["one"] * 17},
            {"tools": [{"type": "function", "function": {"name": 5, "description": "bad", "parameters": {"type": "object"}}}], "warnings": []},
            {"tools": [{"type": "function", "function": {"name": "bad", "description": "bad", "parameters": {"type": object}}}], "warnings": []},
        ]
        for listed in bad_cases:
            with self.subTest(listed=repr(listed)[:80]), patch("aios.agent.toolhost.list_tools", return_value=listed):
                with self.assertRaisesRegex(RuntimeError, "tool host"):
                    agent.AgentSession([{"role": "user", "content": "hello"}], "tools.sock", catalog=[])

    @patch("aios.agent.toolhost.list_tools")
    def test_safe_host_warnings_enter_system_prompt_boundedly(self, list_tools):
        list_tools.return_value = {
            "tools": [clone(BROWSER_TOOL)],
            "warnings": ["Application cache is empty.", "Only approved MCP tools are available."],
        }
        session = agent.AgentSession([{"role": "user", "content": "hello"}], "tools.sock", catalog=[])
        prompt = session.system_prompt()
        self.assertIn("Capability warnings:", prompt)
        self.assertIn("Application cache is empty.", prompt)
        self.assertIn("Only approved MCP tools are available.", prompt)

    @patch("aios.agent.core.load_config", return_value={"mode": "remote", "model": "tool-model"})
    @patch("aios.agent.toolhost.list_tools", return_value={"tools": [clone(APPLICATION_TOOL)], "warnings": []})
    def test_multiple_application_actions_dispatch_in_order_and_preserve_call_ids(self, _list_tools, _load_config):
        catalog = [
            Skill(
                name="application-builder",
                description="Build apps.",
                instructions="Use the cached application tools.",
                allowed_tools=("application",),
                triggers=("calculator",),
            )
        ]
        session = agent.AgentSession([{"role": "user", "content": "calculator"}], "tools.sock", catalog=catalog)
        bodies = []

        def request(route, body):
            bodies.append(clone(body))
            if len(bodies) == 1:
                return stream([
                    {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "application", "arguments": "{\"action\":\"search\",\"query\":\"calculator\"}"}}]},
                    {"tool_calls": [{"index": 1, "id": "call_2", "function": {"name": "application", "arguments": "{\"action\":\"create\",\"title\":\"Calculator\",\"request\":\"calculator\"}"}}]},
                    {"tool_calls": [{"index": 2, "id": "call_3", "function": {"name": "application", "arguments": "{\"action\":\"launch\",\"id\":\"draft-1\"}"}}]},
                ], "tool_calls")
            return stream([{"content": "Application ready."}])

        with patch("aios.agent.core.request", side_effect=request), patch(
            "aios.agent.toolhost.call",
            side_effect=[{"matches": []}, {"id": "draft-1"}, {"launched": True}],
        ) as call:
            events = list(agent.openai_chat(session))

        self.assertEqual(
            call.call_args_list,
            [
                unittest.mock.call("tools.sock", "application", {"action": "search", "query": "calculator"}),
                unittest.mock.call("tools.sock", "application", {"action": "create", "title": "Calculator", "request": "calculator"}),
                unittest.mock.call("tools.sock", "application", {"action": "launch", "id": "draft-1"}),
            ],
        )
        tool_messages = [message for message in bodies[1]["messages"] if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_messages], ["call_1", "call_2", "call_3"])
        self.assertEqual(
            [event["text"] for event in events if event["type"] == "progress"],
            ["Application · search", "Application · create", "Application · launch"],
        )
        self.assertEqual(events[-1], {"type": "token", "text": "Application ready."})

    def test_legacy_exact_browser_sock_works_but_failed_tools_sock_does_not_fallback(self):
        with patch("aios.agent.toolhost.list_tools") as list_tools, patch(
            "aios.agent.browser.call",
            return_value={"snapshot": True},
        ) as browser_call:
            session = agent.AgentSession([{"role": "user", "content": "hello"}], r"C:\private\browser.sock", catalog=[])
            self.assertEqual([tool["function"]["name"] for tool in session.tools()], ["activate_skill", "browser"])
            self.assertEqual(session.dispatch("browser", {"action": "snapshot"}), {"snapshot": True})
        list_tools.assert_not_called()
        browser_call.assert_called_once_with(r"C:\private\browser.sock", {"action": "snapshot"})

        with patch("aios.agent.toolhost.list_tools", side_effect=RuntimeError("Tool host unavailable.")), patch(
            "aios.agent.browser.call"
        ) as browser_call:
            with self.assertRaisesRegex(RuntimeError, "Tool host unavailable"):
                agent.AgentSession([{"role": "user", "content": "hello"}], "tools.sock", catalog=[])
        browser_call.assert_not_called()

    @patch("aios.agent.core.load_config", return_value={"mode": "chatgpt", "model": "ignored"})
    def test_select_provider_and_direct_chatgpt_error(self, _load_config):
        self.assertEqual(agent.select_provider(SimpleNamespace(remote_preferred=False), {"mode": "local"}), ("local", "current"))
        self.assertEqual(agent.select_provider(SimpleNamespace(remote_preferred=True), {"mode": "remote"}), ("remote", "current"))
        self.assertEqual(agent.select_provider(SimpleNamespace(remote_preferred=True), {"mode": "chatgpt"}), ("chatgpt", None))
        with self.assertRaisesRegex(RuntimeError, "ChatGPT.*Task 8"):
            list(agent.chat([{"role": "user", "content": "hello"}], "browser.sock"))

    @patch("aios.agent.core.load_config", return_value={"mode": "local"})
    @patch("aios.agent.toolhost.list_tools", return_value={"tools": [], "warnings": []})
    def test_openai_request_stub_does_not_need_kwargs_yet(self, _list_tools, _load_config):
        session = agent.AgentSession([{"role": "user", "content": "hello"}], "tools.sock", catalog=[])

        def request(route, body):
            self.assertEqual(route, "/chat/completions")
            self.assertTrue(body["stream"])
            return stream([{"content": "Done."}])

        with patch("aios.agent.core.request", side_effect=request):
            events = list(agent.openai_chat(session))
        self.assertEqual(events, [{"type": "token", "text": "Done."}])


if __name__ == "__main__":
    unittest.main()
