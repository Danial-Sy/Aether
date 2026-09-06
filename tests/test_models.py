# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import asyncio
import json
import os
import pathlib
import tempfile
import unittest
import unittest.mock

import httpx

import config
import models as m

GIB = 1024**3

# Trimmed /api/show responses captured from a live Ollama.
QWEN38 = {
    "capabilities": ["completion", "vision", "tools", "thinking"],
    "modified_at": "2026-08-24T23:42:30.605853521-04:00",
    "parameters": "repeat_penalty 1\ntemperature 1\ntop_k 20\ntop_p 0.95",
    "details": {"family": "qwen35", "parameter_size": "27.3B", "quantization_level": "Q4_K_M"},
    "model_info": {
        "general.architecture": "qwen35",
        "general.basename": "Qwen3.8",
        "general.parameter_count": 27320697856,
        "general.size_label": "27B",
        "qwen35.attention.head_count": 24,
        "qwen35.attention.head_count_kv": 4,
        "qwen35.attention.key_length": 256,
        "qwen35.attention.value_length": 256,
        "qwen35.block_count": 65,
        "qwen35.context_length": 262144,
        "qwen35.embedding_length": 5120,
    },
}

CODER = {
    "capabilities": ["completion", "tools"],
    "modified_at": "2026-08-24T20:11:02.000000000-04:00",
    "details": {"family": "qwen3moe", "parameter_size": "30.5B", "quantization_level": "Q4_K_M"},
    "model_info": {
        "general.architecture": "qwen3moe",
        "general.basename": "Qwen3-Coder",
        "general.parameter_count": 30532122624,
        "general.size_label": "30B-A3B",
        "qwen3moe.attention.head_count": 32,
        "qwen3moe.attention.head_count_kv": 4,
        "qwen3moe.attention.key_length": 128,
        "qwen3moe.attention.value_length": 128,
        "qwen3moe.block_count": 48,
        "qwen3moe.context_length": 262144,
        "qwen3moe.embedding_length": 2048,
    },
}

# No key_length/value_length, which is the common case.
TINY = {
    "capabilities": ["completion", "tools"],
    "modified_at": "2026-08-01T00:00:00.000000000-04:00",
    "details": {"family": "qwen2", "parameter_size": "494.03M", "quantization_level": "Q4_K_M"},
    "model_info": {
        "general.architecture": "qwen2",
        "general.basename": "Qwen2.5",
        "general.size_label": "0.5B",
        "qwen2.attention.head_count": 14,
        "qwen2.attention.head_count_kv": 2,
        "qwen2.block_count": 24,
        "qwen2.context_length": 32768,
        "qwen2.embedding_length": 896,
    },
}


class KvMathTests(unittest.TestCase):
    def test_explicit_key_and_value_lengths_are_used(self):
        self.assertEqual(m.kv_bytes_per_token(QWEN38["model_info"]), 65 * 4 * 512)

    def test_head_dim_falls_back_to_embedding_over_head_count(self):
        self.assertEqual(m.kv_bytes_per_token(TINY["model_info"]), 24 * 2 * 128)

    def test_missing_metadata_reports_unknown_rather_than_guessing(self):
        self.assertEqual(m.kv_bytes_per_token({"general.architecture": "mystery"}), 0)


class CapabilityTests(unittest.TestCase):
    def test_capabilities_come_from_ollama_not_a_hardcoded_list(self):
        vision = m.profile_from_show("qwen3.8:27b", QWEN38)
        coder = m.profile_from_show("qwen3-coder:30b", CODER)

        self.assertEqual((vision["tools"], vision["vision"], vision["think"]), (True, True, True))
        self.assertEqual((coder["tools"], coder["vision"], coder["think"]), (True, False, False))

    def test_context_max_is_read_from_the_gguf(self):
        self.assertEqual(m.profile_from_show("x", QWEN38)["context_max"], 262144)
        self.assertEqual(m.profile_from_show("x", TINY)["context_max"], 32768)

    def test_context_max_falls_back_when_model_info_is_absent(self):
        self.assertEqual(m.profile_from_show("x", {})["context_max"], m.FALLBACK_CONTEXT_MAX)


class SamplingTests(unittest.TestCase):
    def test_modelfile_parameters_are_parsed(self):
        parsed = m.parse_parameters("temperature 0.6\ntop_k 40\nstop \"<|im_end|>\"")

        self.assertEqual(parsed, {"temperature": 0.6, "top_k": 40})

    def test_catalog_tuning_wins_over_the_models_own_parameters(self):
        # The GGUF ships temperature 1; Aether's tuned value must survive.
        profile = m.profile_from_show(
            "qwen3.8:27b", QWEN38, entry={"tuning": {"temperature": 0.7, "top_p": 0.95}}
        )

        self.assertEqual(profile["temperature"], 0.7)
        self.assertEqual(profile["top_k"], 20)

    def test_probe_parameters_are_used_when_the_catalog_says_nothing(self):
        self.assertEqual(m.profile_from_show("x", QWEN38)["temperature"], 1.0)


class FitTests(unittest.TestCase):
    def _default_agent(self):
        return m.profile_from_show(
            "qwen3.8:27b",
            QWEN38,
            weights_bytes=int(15.7 * GIB),
            entry=m.catalog_entry("qwen3.8:27b"),
        )

    def test_measured_kv_overrides_the_generic_formula(self):
        profile = self._default_agent()

        self.assertEqual(profile["kv_source"], "measured")
        self.assertEqual(profile["kv_bytes_per_token"], 88500)

    def test_the_default_agent_still_gets_64k_on_a_24gb_card(self):
        # DEFAULT_SETTINGS["agent_context"], which this must not regress.
        self.assertEqual(m.max_context(self._default_agent(), 24 * GIB), 65536)

    def test_the_computed_formula_alone_would_have_undersized_it(self):
        computed = m.profile_from_show("qwen3.8:27b", QWEN38, weights_bytes=int(15.7 * GIB))

        self.assertEqual(computed["kv_source"], "computed")
        self.assertLess(m.max_context(computed, 24 * GIB), 65536)

    def test_a_bigger_card_gets_a_bigger_window(self):
        profile = self._default_agent()

        self.assertGreater(m.max_context(profile, 48 * GIB), m.max_context(profile, 24 * GIB))

    def test_context_never_exceeds_the_native_window(self):
        profile = m.profile_from_show("x", TINY, weights_bytes=int(0.4 * GIB))

        self.assertLessEqual(m.max_context(profile, 80 * GIB), 32768)

    def test_a_model_that_cannot_load_reports_zero(self):
        profile = m.profile_from_show("x", QWEN38, weights_bytes=int(40 * GIB))

        self.assertEqual(m.max_context(profile, 8 * GIB), 0)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = m.load_catalog()

    def test_catalog_loads(self):
        self.assertTrue(self.catalog.get("models"))
        self.assertTrue(self.catalog.get("tiers"))

    def test_every_tier_and_utility_tag_has_a_catalog_entry(self):
        referenced = {e["tag"] for e in self.catalog.get("utility") or []}
        for tier in self.catalog["tiers"]:
            referenced.update(entry["tag"] for entry in tier["models"])

        self.assertTrue(referenced <= set(self.catalog["models"]), referenced - set(self.catalog["models"]))

    def test_tiers_cover_every_size_without_a_gap(self):
        tiers = self.catalog["tiers"]
        self.assertEqual(float(tiers[0]["min_gb"]), 0.0)
        self.assertIsNone(tiers[-1]["max_gb"])
        for lower, upper in zip(tiers, tiers[1:]):
            self.assertEqual(float(lower["max_gb"]), float(upper["min_gb"]))

    def test_a_24gb_card_is_recommended_the_pair_aether_was_tuned_on(self):
        tags = {entry["tag"] for entry in m.recommend(24, self.catalog)["models"]}

        self.assertEqual(tags, {"qwen3.8:27b", "qwen3-coder:30b"})

    def test_a_small_card_is_recommended_one_model_for_both_roles(self):
        tier = m.recommend(5, self.catalog)

        self.assertEqual(len(tier["models"]), 1)
        self.assertEqual(tier["models"][0]["roles"], ["chat", "agentic"])

    def test_recommendation_scales_up_past_the_authors_own_machine(self):
        self.assertNotEqual(m.recommend(80, self.catalog)["id"], m.recommend(24, self.catalog)["id"])

    def test_every_agentic_recommendation_claims_tool_support(self):
        for tier in self.catalog["tiers"]:
            for entry in tier["models"]:
                if "agentic" not in entry["roles"]:
                    continue
                hints = self.catalog["models"][entry["tag"]]["hints"]
                self.assertTrue(hints["tools"], entry["tag"])

    def test_search_ranks_the_default_model_first(self):
        self.assertEqual(m.search_catalog("", self.catalog)[0]["tag"], "qwen3.8:27b")

    def test_search_matches_description_and_tags_not_just_the_name(self):
        tags = {hit["tag"] for hit in m.search_catalog("vision", self.catalog)}

        self.assertIn("gemma3:12b", tags)
        self.assertNotIn("qwen2.5-coder:7b", tags)


def _ollama_is_up() -> bool:
    try:
        httpx.get(f"{m.OLLAMA_HOST}/api/tags", timeout=2.0).raise_for_status()
        return True
    except Exception:
        return False


LIVE = _ollama_is_up()


@unittest.skipUnless(LIVE, "needs a running Ollama")
class LiveContractTests(unittest.TestCase):
    """The fixtures above are captures. These assert Ollama still answers that shape."""

    @classmethod
    def setUpClass(cls):
        tags = httpx.get(f"{m.OLLAMA_HOST}/api/tags", timeout=10.0).json()["models"]
        cls.installed = {t["name"]: t.get("size", 0) for t in tags}
        cls.shown = {
            name: httpx.post(f"{m.OLLAMA_HOST}/api/show", json={"model": name}, timeout=30.0).json()
            for name in cls.installed
        }

    def test_show_still_reports_capabilities(self):
        for name, show in self.shown.items():
            self.assertIsInstance(show.get("capabilities"), list, name)
            self.assertIn("completion", show["capabilities"], name)

    def test_show_still_carries_the_fields_the_fit_math_reads(self):
        for name, show in self.shown.items():
            info = show.get("model_info") or {}
            arch = info.get("general.architecture")
            self.assertTrue(arch, name)
            self.assertTrue(info.get(f"{arch}.block_count"), name)
            self.assertTrue(info.get(f"{arch}.attention.head_count_kv"), name)
            self.assertTrue(info.get(f"{arch}.context_length"), name)

    def test_every_installed_model_profiles_and_sizes(self):
        for name, size in self.installed.items():
            profile = m.profile_from_show(
                name, self.shown[name], weights_bytes=size, entry=m.catalog_entry(name)
            )
            self.assertGreater(profile["kv_bytes_per_token"], 0, name)
            self.assertGreater(profile["context_max"], 0, name)
            self.assertGreater(m.max_context(profile, 24 * GIB), 0, name)

    def test_the_tuned_pair_keeps_its_configured_sampling(self):
        from config import MODELS

        for model_id, meta in MODELS.items():
            if model_id not in self.shown:
                continue
            profile = m.profile_from_show(
                model_id, self.shown[model_id], entry=m.catalog_entry(model_id)
            )
            for field in ("temperature", "top_p", "top_k", "repeat_penalty"):
                self.assertEqual(profile[field], meta[field], f"{model_id}.{field}")


@unittest.skipUnless(os.environ.get("AETHER_LIVE_TESTS") == "1", "set AETHER_LIVE_TESTS=1")
class RegistryTests(unittest.TestCase):
    def test_every_catalog_tag_resolves_to_a_real_download(self):
        catalog = m.load_catalog()
        for tag in catalog["models"]:
            size = asyncio.run(m.fetch_manifest_size(tag))
            self.assertGreater(size, 0, tag)

    def test_an_unknown_tag_reports_zero_rather_than_raising(self):
        self.assertEqual(asyncio.run(m.fetch_manifest_size("not-a-real-model:9b")), 0)


def _reg(**models):
    base = {"tools": True, "vision": False, "think": False, "context_max": 32768}
    return {mid: {**base, "id": mid, "label": mid, **over} for mid, over in models.items()}


class RoleTests(unittest.TestCase):
    def test_one_model_serves_every_role(self):
        reg = _reg(solo={"think": True})

        self.assertEqual(m.models_for("chat", {}, reg), ["solo"])
        self.assertEqual(m.models_for("agentic", {}, reg), ["solo"])
        self.assertEqual(m.models_for("reasoning", {}, reg), ["solo"])
        self.assertEqual(m.active("chat", {}, reg), "solo")
        self.assertEqual(m.active("agentic", {}, reg), "solo")

    def test_a_model_without_tools_never_reaches_the_agentic_role(self):
        reg = _reg(blind={"tools": False})

        self.assertEqual(m.models_for("chat", {}, reg), ["blind"])
        self.assertEqual(m.models_for("agentic", {}, reg), [])
        self.assertIsNone(m.active("agentic", {}, reg))

    def test_assigning_agentic_to_a_toolless_model_is_refused(self):
        reg = _reg(blind={"tools": False})
        settings = {"model_roles": {"blind": ["chat", "agentic"]}}

        self.assertEqual(m.role_map(settings, reg)["blind"], ["chat"])
        self.assertEqual(m.models_for("agentic", settings, reg), [])

    def test_reasoning_needs_thinking_not_an_assignment(self):
        reg = _reg(plain={}, thinker={"think": True})

        self.assertEqual(m.models_for("reasoning", {}, reg), ["thinker"])

    def test_an_explicit_assignment_overrides_the_capability_fallback(self):
        reg = _reg(a={}, b={})
        settings = {"model_roles": {"b": ["chat"]}}

        self.assertEqual(m.models_for("agentic", settings, reg), ["a"])

    def test_computer_use_routes_to_the_model_with_eyes(self):
        reg = _reg(blind={}, seer={"vision": True})

        self.assertEqual(m.vision_model({}, reg), "seer")

    def test_computer_use_is_unavailable_when_nothing_can_see(self):
        self.assertIsNone(m.vision_model({}, _reg(blind={})))

    def test_the_last_picked_model_wins_over_the_default_preference(self):
        reg = _reg(a={}, b={})

        self.assertEqual(m.active("chat", {"model_active": {"chat": "b"}}, reg), "b")

    def test_a_stale_pick_falls_back_instead_of_breaking(self):
        reg = _reg(a={})

        self.assertEqual(m.active("chat", {"model_active": {"chat": "deleted"}}, reg), "a")

    def test_the_default_pair_keeps_its_old_division_of_labour(self):
        reg = m.installed()
        if config.DEFAULT_CODER_MODEL not in reg:
            self.skipTest("default pair not installed")
        # Was DEFAULT_AGENT_KEY = "coder" and VISION_AGENT_KEY = "agent".
        self.assertEqual(m.active("agentic", {}, reg), config.DEFAULT_CODER_MODEL)
        self.assertEqual(m.vision_model({}, reg), config.DEFAULT_AGENT_MODEL)


class ResolveTests(unittest.TestCase):
    def test_reasoning_runs_the_chat_model_cooler(self):
        reg = _reg(x={"temperature": 0.7, "top_k": 20})

        chat = m.resolve("x", "chat", {}, reg)
        deep = m.resolve("x", "reasoning", {}, reg)

        self.assertEqual((chat["temperature"], chat["top_k"]), (0.7, 20))
        self.assertEqual((deep["temperature"], deep["top_k"]), (0.6, 40))

    def test_effort_follows_the_role_not_the_model(self):
        reg = _reg(x={})

        self.assertEqual(m.resolve("x", "chat", {}, reg)["effort"], "high")
        self.assertEqual(m.resolve("x", "reasoning", {}, reg)["effort"], "medium")

    def test_agentic_wants_a_bigger_window_than_chat_on_the_same_model(self):
        reg = _reg(x={"context_max": 262144})

        self.assertGreater(
            m.context_limit("x", "agentic", {}, reg["x"]),
            m.context_limit("x", "chat", {}, reg["x"]),
        )

    def test_a_per_model_override_beats_the_role_default(self):
        reg = _reg(x={"context_max": 262144})
        settings = {"role_context": {"agentic": 65536}, "model_context": {"x": 16384}}

        self.assertEqual(m.context_limit("x", "agentic", settings, reg["x"]), 16384)

    def test_the_window_never_exceeds_what_the_model_supports(self):
        reg = _reg(x={"context_max": 8192})
        settings = {"role_context": {"agentic": 131072}}

        self.assertEqual(m.context_limit("x", "agentic", settings, reg["x"]), 8192)

    def test_the_vision_paragraph_is_added_only_for_a_model_with_eyes(self):
        seer = m.system_prompt("agentic", {"vision": True})
        blind = m.system_prompt("agentic", {"vision": False})

        self.assertIn("see images", seer)
        self.assertNotIn("see images", blind)
        self.assertNotIn("see images", m.system_prompt("chat", {"vision": True}))

    def test_no_prompt_names_a_specific_model(self):
        for role in config.ROLES:
            self.assertNotIn("Qwen", m.system_prompt(role, {"vision": True}), role)


class TagTests(unittest.TestCase):
    def test_bare_names_get_the_latest_tag(self):
        self.assertEqual(m.normalize_tag("qwen3"), "qwen3:latest")

    def test_registry_prefixes_are_stripped(self):
        self.assertEqual(m.normalize_tag("registry.ollama.ai/library/qwen3:8b"), "qwen3:8b")

    def test_a_hugging_face_tag_keeps_its_capitals(self):
        # Ollama stores the repository's own spelling, so folding the case
        # here would stop an installed model matching itself.
        tag = "hf.co/bartowski/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"
        self.assertEqual(m.normalize_tag(tag), tag)

    def test_a_hugging_face_repo_is_never_given_a_latest_tag(self):
        # There is no `latest` on Hugging Face: the part after the colon is a
        # quantization, and inventing one gets "model not found" from the pull.
        self.assertEqual(
            m.normalize_tag("hf.co/bartowski/Qwen_Qwen3-8B-GGUF"),
            "hf.co/bartowski/Qwen_Qwen3-8B-GGUF",
        )


class CacheTests(unittest.TestCase):
    def test_a_rebuilt_model_invalidates_the_cache(self):
        cached = {"modified_at": "2026-08-24T23:42:30Z"}

        self.assertTrue(m.cache_is_current(cached, "2026-08-24T23:42:30Z"))
        self.assertFalse(m.cache_is_current(cached, "2026-09-01T00:00:00Z"))

    def test_a_model_with_no_build_stamp_is_never_considered_current(self):
        self.assertFalse(m.cache_is_current({"modified_at": ""}, ""))


if __name__ == "__main__":
    unittest.main()


class ThinkingTagTests(unittest.TestCase):
    """Ollama gives most models a native `thinking` field, but some emit tags."""

    def setUp(self):
        try:
            import server
        except ModuleNotFoundError:
            self.skipTest("server dependencies are installed in Aether's .venv")
        self.split = server._split_thinking

    def test_every_known_tag_family_is_recognised(self):
        for tag in ("think", "thinking", "reasoning"):
            body, think = self.split(f"<{tag}>reasoned</{tag}>answer")
            self.assertEqual((body, think), ("answer", "reasoned"), tag)

    def test_tags_are_case_insensitive(self):
        self.assertEqual(self.split("<THINK>a</THINK>b"), ("b", "a"))

    def test_an_unclosed_tag_means_the_reply_was_cut_off(self):
        self.assertEqual(self.split("<think>ran out"), ("", "ran out"))

    def test_ordinary_markup_is_left_alone(self):
        text = "here is <b>bold</b> and <div>markup</div>"
        self.assertEqual(self.split(text), (text, ""))

    def test_a_reply_with_no_tags_is_untouched(self):
        self.assertEqual(self.split("just an answer"), ("just an answer", ""))


class IdentityPromptTests(unittest.TestCase):
    def test_the_prompt_names_the_model_that_is_actually_answering(self):
        prompt = m.system_prompt("chat", {"label": "Llama 3.1 8B"})

        self.assertIn("Llama 3.1 8B", prompt)

    def test_an_unprofiled_model_gets_no_identity_line_rather_than_a_wrong_one(self):
        self.assertNotIn("running", m.system_prompt("chat", {}).split("Honor")[0][-40:])

    def test_the_identity_line_follows_the_model_not_the_role(self):
        for role in config.ROLES:
            self.assertIn("Gemma 3 12B", m.system_prompt(role, {"label": "Gemma 3 12B"}), role)


class RegistryRefreshTests(unittest.IsolatedAsyncioTestCase):
    """Startup profiles every installed model and drops any that vanished."""

    def _patch(self, tags, shows, cache):
        async def fake_show(model_id, **kw):
            return shows[model_id]

        client = unittest.mock.MagicMock()
        resp = unittest.mock.MagicMock()
        resp.json.return_value = {"models": tags}
        resp.raise_for_status.return_value = None

        class Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return resp

        saved = {}
        return (
            unittest.mock.patch.object(m, "fetch_show", fake_show),
            unittest.mock.patch.object(httpx, "AsyncClient", Client),
            unittest.mock.patch.object(m, "load_cache", lambda: cache),
            unittest.mock.patch.object(m, "save_cache", lambda c: saved.update(c)),
        ), saved

    async def test_a_model_removed_from_ollama_leaves_the_registry(self):
        cache = {"profiles": {"gone:7b": {"id": "gone:7b", "modified_at": "x"},
                              "kept:7b": {"id": "kept:7b", "modified_at": "x"}}}
        patches, saved = self._patch(
            [{"name": "kept:7b", "size": 100}], {"kept:7b": dict(TINY)}, cache
        )
        with patches[0], patches[1], patches[2], patches[3]:
            await m.refresh_registry()

        self.assertIn("kept:7b", saved["profiles"])
        self.assertNotIn("gone:7b", saved["profiles"])

    async def test_a_newly_pulled_model_is_profiled(self):
        cache = {"profiles": {}}
        patches, saved = self._patch(
            [{"name": "new:7b", "size": 4096}], {"new:7b": dict(TINY)}, cache
        )
        with patches[0], patches[1], patches[2], patches[3]:
            await m.refresh_registry()

        self.assertEqual(saved["profiles"]["new:7b"]["weights_bytes"], 4096)
        self.assertTrue(saved["profiles"]["new:7b"]["tools"])

    async def test_an_unreachable_ollama_never_empties_the_registry(self):
        # Pruning reads the tag list as the truth about what is installed, so a
        # failed fetch has to mean "unknown", not "nothing left".
        cache = {"profiles": {"kept:7b": {"id": "kept:7b", "modified_at": "x"}}}
        saved = {}

        class Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                raise httpx.ConnectError("Ollama is not running")

        with unittest.mock.patch.object(httpx, "AsyncClient", Client), \
             unittest.mock.patch.object(m, "load_cache", lambda: cache), \
             unittest.mock.patch.object(m, "save_cache", lambda c: saved.update(c)):
            registry = await m.refresh_registry()

        self.assertEqual(saved, {})
        self.assertIn("kept:7b", cache["profiles"])
        self.assertIn("kept:7b", registry)

    async def test_an_unchanged_model_is_not_reprofiled(self):
        stamp = TINY["modified_at"]
        cache = {"profiles": {"kept:7b": {"id": "kept:7b", "modified_at": stamp, "marker": 1}}}
        patches, saved = self._patch(
            [{"name": "kept:7b", "size": 1}], {"kept:7b": dict(TINY)}, cache
        )
        with patches[0], patches[1], patches[2], patches[3]:
            await m.refresh_registry()

        # Nothing changed, so nothing was written.
        self.assertEqual(saved, {})


class ProbeCacheTests(unittest.TestCase):
    """The agent loop reads this once per step, so it is memoized on the file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "models.json"
        patcher = unittest.mock.patch.object(m, "PROFILE_CACHE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        m._cache_memo = None

    def test_a_repeated_read_does_not_touch_the_disk_again(self):
        m.save_cache({"profiles": {"a": {}}})

        self.assertIs(m.load_cache(), m.load_cache())

    def test_a_model_installed_mid_session_is_picked_up(self):
        m.save_cache({"profiles": {"a": {}}})
        self.assertEqual(list(m.load_cache()["profiles"]), ["a"])

        # Another process (or a pull) rewrites the cache underneath us.
        self.path.write_text(json.dumps({"profiles": {"a": {}, "b": {}}}), encoding="utf-8")
        m._cache_memo = (m._cache_memo[0][0] - 1, m._cache_memo[1])

        self.assertEqual(sorted(m.load_cache()["profiles"]), ["a", "b"])

    def test_saving_refreshes_what_the_next_read_sees(self):
        m.save_cache({"profiles": {"a": {}}})
        m.save_cache({"profiles": {"a": {}, "c": {}}})

        self.assertEqual(sorted(m.load_cache()["profiles"]), ["a", "c"])

    def test_a_missing_cache_reads_as_empty_rather_than_raising(self):
        self.assertEqual(m.load_cache(), {})


class DefaultParityTests(unittest.TestCase):
    """The tuned pair must resolve exactly as it did before modularity.

    Captured from config.py at commit 8052512, the last release before roles.
    Everything else grows off this, so a drift here is a real regression even
    when every other test passes.
    """

    GOLDEN = {
        ("general", "chat", config.DEFAULT_CHAT_MODEL): {
            "label": "Qwen3.8 27B", "color": "#ff4d6d", "keep_alive": "30m",
            "temperature": 0.7, "top_p": 0.95, "top_k": 20, "repeat_penalty": 1.0,
            "vision": True, "think": True, "effort": "high",
            "context_max": 262144, "context": 32768,
        },
        ("reasoning", "reasoning", config.DEFAULT_CHAT_MODEL): {
            "label": "Qwen3.8 27B (Reasoning)", "color": "#ff2d55", "keep_alive": "30m",
            "temperature": 0.6, "top_p": 0.95, "top_k": 40, "repeat_penalty": 1.05,
            "vision": True, "think": True, "effort": "medium",
            "context_max": 131072, "context": 32768,
        },
        ("agent", "agentic", config.DEFAULT_AGENT_MODEL): {
            "label": "Qwen3.8 27B", "color": "#ff4d6d", "keep_alive": "30m",
            "temperature": 0.7, "top_p": 0.95, "top_k": 20, "repeat_penalty": 1.0,
            "vision": True, "think": True, "effort": "high",
            "context_max": 262144, "context": 65536,
        },
        ("coder", "agentic", config.DEFAULT_CODER_MODEL): {
            "label": "Qwen3-Coder 30B", "color": "#00d4ff", "keep_alive": "30m",
            "temperature": 0.7, "top_p": 0.8, "top_k": 20, "repeat_penalty": 1.05,
            "vision": False, "think": False, "effort": "high",
            "context_max": 262144, "context": 65536,
        },
    }

    def test_every_knob_matches_the_pre_modularity_release(self):
        settings = {"role_context": dict(config.ROLE_CONTEXT)}
        registry = m.installed()
        for (legacy, role, model_id), expected in self.GOLDEN.items():
            resolved = m.resolve(model_id, role, settings, registry)
            for field, want in expected.items():
                self.assertEqual(resolved[field], want, f"{legacy}.{field}")

    def test_reasoning_stays_capped_below_the_models_native_window(self):
        # Thinking fills KV fast, so 262144 was never offered for reasoning.
        settings = {"role_context": {"reasoning": 262144}}
        resolved = m.resolve(config.DEFAULT_CHAT_MODEL, "reasoning", settings, m.installed())

        self.assertEqual(resolved["context"], 131072)

    def test_the_cap_holds_on_the_raw_profile_too(self):
        # server._ctx_limit calls context_limit directly with the registry's
        # profile, not the role-resolved meta, so the cap must live there too.
        raw = {"id": "x", "label": "x", "context_max": 262144, "think": True}
        settings = {"role_context": {"reasoning": 262144, "chat": 262144}}

        self.assertEqual(m.context_limit("x", "reasoning", settings, raw), 131072)
        self.assertEqual(m.context_limit("x", "chat", settings, raw), 262144)

    def test_a_role_cap_never_raises_a_smaller_model(self):
        tiny = {"x": {"id": "x", "label": "x", "context_max": 8192, "think": True}}
        resolved = m.resolve("x", "reasoning", {"role_context": {"reasoning": 262144}}, tiny)

        self.assertEqual(resolved["context"], 8192)

    def test_reasoning_is_distinguishable_from_plain_chat_on_any_model(self):
        reg = {"x": {"id": "x", "label": "Gemma 3 12B", "context_max": 32768, "think": True}}
        chat = m.resolve("x", "chat", {}, reg)
        deep = m.resolve("x", "reasoning", {}, reg)

        self.assertNotEqual(chat["label"], deep["label"])
        self.assertNotEqual(chat["color"], deep["color"])

    def test_the_suffix_is_not_applied_twice(self):
        reg = {"x": {"id": "x", "label": "M", "context_max": 32768, "think": True}}
        once = m.resolve("x", "reasoning", {}, reg)
        twice = m.resolve("x", "reasoning", {}, {"x": once})

        self.assertEqual(once["label"], twice["label"])


class EffortDisclosureTests(unittest.TestCase):
    """Users could not tell which effort levels actually think.

    With one model family that was a documentation problem. With any Ollama
    model it is a correctness one: the default agentic model cannot think, so
    the old hints claimed a reasoning pass that never happened.
    """

    def test_a_thinking_model_marks_exactly_the_thinking_tiers(self):
        levels = {lv["key"]: lv for lv in m.effort_levels({"think": True})}

        self.assertFalse(levels["minimal"]["think"])
        self.assertFalse(levels["low"]["think"])
        self.assertTrue(levels["medium"]["think"])
        self.assertTrue(levels["high"]["think"])
        self.assertTrue(levels["max"]["think"])

    def test_a_model_without_thinking_marks_no_tier_at_all(self):
        for lv in m.effort_levels({"think": False}):
            self.assertFalse(lv["think"], lv["key"])

    def test_every_level_states_whether_it_thinks(self):
        for profile in ({"think": True}, {"think": False}):
            for lv in m.effort_levels(profile):
                self.assertRegex(lv["hint"], r"Thinking (on|off)\.$|no thinking mode",
                                 f"{lv['key']} / think={profile['think']}")

    def test_a_blind_model_never_claims_a_reasoning_pass(self):
        for lv in m.effort_levels({"think": False}):
            self.assertNotIn("Thinking on", lv["hint"])

    def test_the_tiers_come_back_low_to_high_for_the_slider(self):
        self.assertEqual([lv["key"] for lv in m.effort_levels({})], list(config.EFFORT_ORDER))

    def test_the_summary_names_the_first_thinking_tier(self):
        summary = m.thinking_summary({"think": True})

        self.assertTrue(summary["supported"])
        self.assertEqual(summary["from"], "medium")
        self.assertIn("Medium", summary["summary"])

    def test_the_summary_is_explicit_when_the_model_cannot_think(self):
        summary = m.thinking_summary({"think": False})

        self.assertFalse(summary["supported"])
        self.assertIsNone(summary["from"])
        self.assertIn("no thinking mode", summary["summary"])

    def test_the_hints_never_hardcode_thinking_since_it_depends_on_the_model(self):
        for key, tier in config.EFFORT.items():
            self.assertNotIn("hink", tier["hint"], key)

    def test_the_default_agentic_model_is_reported_honestly(self):
        registry = m.installed()
        if config.DEFAULT_CODER_MODEL not in registry:
            self.skipTest("default pair not installed")
        coder = m.resolve(config.DEFAULT_CODER_MODEL, "agentic", {}, registry)

        self.assertFalse(m.thinking_summary(coder)["supported"])
        self.assertTrue(all(not lv["think"] for lv in m.effort_levels(coder)))


class PullProgressTests(unittest.TestCase):
    """Ollama reports a pull per layer, which is not what a progress bar wants."""

    # The shape a real pull streams: a layer's total lands before its bytes.
    STREAM = [
        {"status": "pulling manifest"},
        {"status": "pulling weights", "digest": "w", "total": 1_000_000},
        {"status": "pulling weights", "digest": "w", "total": 1_000_000, "completed": 400_000},
        {"status": "pulling weights", "digest": "w", "total": 1_000_000, "completed": 1_000_000},
        {"status": "pulling license", "digest": "l", "total": 1_000, "completed": 1_000},
        {"status": "verifying sha256 digest"},
        {"status": "writing manifest"},
        {"status": "success"},
    ]

    def _run(self):
        p = m.PullProgress()
        return [p.update(c) for c in self.STREAM]

    def test_bytes_never_go_backwards(self):
        completed = [e["completed"] for e in self._run()]

        self.assertEqual(completed, sorted(completed))

    def test_progress_is_summed_across_layers_not_reported_per_layer(self):
        events = self._run()

        # The licence layer must not reset the bar to its own 100%.
        self.assertEqual(events[4]["total"], 1_001_000)
        self.assertEqual(events[4]["completed"], 1_001_000)

    def test_the_pull_ends_at_a_hundred_percent(self):
        self.assertEqual(self._run()[-1]["pct"], 100.0)

    def test_a_status_line_with_no_bytes_keeps_the_last_figure(self):
        events = self._run()

        self.assertEqual(events[-2]["status"], "writing manifest")
        self.assertEqual(events[-2]["completed"], 1_001_000)

    def test_before_any_layer_there_is_no_percentage_to_show(self):
        self.assertIsNone(self._run()[0]["pct"])

    def test_a_late_chunk_cannot_undo_progress_already_reported(self):
        # Chunks can arrive out of order or repeat a stale figure; the bar must
        # not run backwards inside one layer.
        p = m.PullProgress()
        p.update({"digest": "w", "total": 100, "completed": 90})
        back = p.update({"digest": "w", "total": 100, "completed": 10})

        self.assertEqual(back["completed"], 90)
        self.assertEqual(back["total"], 100)

    def test_a_layer_total_that_shrinks_is_ignored(self):
        p = m.PullProgress()
        p.update({"digest": "w", "total": 100, "completed": 50})
        shrunk = p.update({"digest": "w", "total": 10, "completed": 50})

        self.assertEqual(shrunk["total"], 100)

    def test_a_repeated_chunk_does_not_double_count(self):
        p = m.PullProgress()
        chunk = {"status": "pulling weights", "digest": "w", "total": 100, "completed": 50}
        p.update(chunk)
        second = p.update(chunk)

        self.assertEqual(second["completed"], 50)
        self.assertEqual(second["total"], 100)


class HardwareTests(unittest.TestCase):
    def test_the_probe_answers_every_field_the_ui_reads(self):
        hw = m.hardware()

        for field in ("gpu", "vram_bytes", "ram_bytes", "free_disk_bytes",
                      "model_store", "usable_bytes"):
            self.assertIn(field, hw)

    def test_vram_decides_when_a_gpu_is_present(self):
        hw = m.hardware()
        if not hw["vram_bytes"]:
            self.skipTest("no GPU on this machine")
        self.assertEqual(hw["usable_bytes"], hw["vram_bytes"])

    def test_the_store_path_is_the_disk_a_pull_actually_fills(self):
        self.assertTrue(str(m.model_store()).endswith("models"))

    def test_fit_reports_what_the_window_would_be(self):
        profile = m.profile_from_show("x", QWEN38, weights_bytes=int(15.7 * GIB),
                                      entry=m.catalog_entry(config.DEFAULT_CHAT_MODEL))
        report = m.fit_report(profile, 24 * GIB)

        self.assertTrue(report["fits"])
        self.assertEqual(report["max_context"], 65536)
        self.assertEqual(report["kv_source"], "measured")

    def test_a_model_too_big_for_the_card_is_reported_as_not_fitting(self):
        profile = m.profile_from_show("x", QWEN38, weights_bytes=int(40 * GIB))
        report = m.fit_report(profile, 8 * GIB)

        self.assertFalse(report["fits"])
        self.assertEqual(report["max_context"], 0)


class RecommendBudgetTests(unittest.TestCase):
    """What to recommend is not the same question as what will fit."""

    def _hw(self, vram_gb, ram_gb):
        with unittest.mock.patch.object(m, "_nvidia_vram_bytes",
                                        return_value=(int(vram_gb * GIB), "Test GPU")), \
             unittest.mock.patch.object(m, "_total_ram_bytes", return_value=int(ram_gb * GIB)), \
             unittest.mock.patch.object(m, "_free_disk_bytes", return_value=500 * GIB):
            return m.hardware()

    def test_a_gpu_machine_recommends_against_its_vram(self):
        hw = self._hw(24, 32)

        self.assertFalse(hw["cpu_only"])
        self.assertEqual(hw["recommend_bytes"], hw["vram_bytes"])

    def test_a_cpu_machine_is_capped_far_below_what_would_fit(self):
        # 128 GB of RAM would hold a 70B. It would answer at about a word a
        # second, so the recommendation must not be sized off RAM.
        hw = self._hw(0, 128)

        self.assertTrue(hw["cpu_only"])
        self.assertEqual(hw["usable_bytes"], 128 * GIB)
        self.assertEqual(hw["recommend_bytes"], m.CPU_RECOMMEND_CAP)
        self.assertLess(hw["recommend_bytes"], hw["usable_bytes"])

    def test_a_small_cpu_machine_is_not_given_more_than_it_has(self):
        hw = self._hw(0, 4)

        self.assertEqual(hw["recommend_bytes"], 4 * GIB)

    def test_the_cap_lands_a_cpu_machine_on_a_model_that_stays_usable(self):
        hw = self._hw(0, 64)
        tier = m.recommend(hw["recommend_bytes"] / GIB)

        self.assertEqual(tier["id"], "small")
        for entry in tier["models"]:
            self.assertNotIn("27b", entry["tag"])
            self.assertNotIn("70b", entry["tag"])


class InstallPlanTests(unittest.TestCase):
    """One plan, shared by both installers, so neither carries its own catalog."""

    def _plan(self, vram_gb, installed=frozenset(), size=5 * GIB):
        usable = int(vram_gb * GIB)
        hw = {"gpu": "Test", "vram_bytes": usable, "ram_bytes": 32 * GIB,
              "free_disk_bytes": 500 * GIB, "model_store": "/models", "cpu_only": False,
              "usable_bytes": usable, "recommend_bytes": usable}
        with unittest.mock.patch.object(m, "hardware", return_value=hw), \
             unittest.mock.patch.object(m, "manifest_size", side_effect=lambda t, **k: size):
            return m.install_plan(set(installed))

    def test_the_plan_carries_both_choices_and_the_hardware(self):
        plan = self._plan(24)

        self.assertEqual(plan["default"]["id"], "default")
        self.assertIn("hardware", plan)
        self.assertIn("ollama.com/library", plan["library_url"])

    def test_the_download_size_is_the_sum_of_what_is_missing(self):
        plan = self._plan(24, size=5 * GIB)

        self.assertEqual(len(plan["default"]["missing"]), len(plan["default"]["tags"]))
        self.assertEqual(plan["default"]["download_bytes"], len(plan["default"]["tags"]) * 5 * GIB)

    def test_models_already_installed_are_not_counted(self):
        plan = self._plan(24, installed=set(m.load_catalog()["models"]))

        self.assertEqual(plan["default"]["missing"], [])
        self.assertEqual(plan["default"]["download_bytes"], 0)

    def test_a_24gb_card_is_told_the_two_choices_are_the_same(self):
        self.assertTrue(self._plan(24)["same"])

    def test_a_small_card_is_told_they_differ(self):
        plan = self._plan(6)

        self.assertFalse(plan["same"])
        self.assertNotIn(config.DEFAULT_CHAT_MODEL, plan["recommended"]["tags"])

    def test_the_title_model_rides_along_with_every_choice(self):
        for vram in (4, 24, 80):
            plan = self._plan(vram)
            for key in ("default", "recommended"):
                self.assertIn(config.TITLE_MODEL_ID, plan[key]["tags"], f"{vram}/{key}")
