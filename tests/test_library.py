# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import asyncio
import os
import unittest
import unittest.mock

import library as L

GIB = 1024**3

# One card trimmed from a live ollama.com/library, keeping every field the
# parser reads and the class soup it has to read them through.
LIBRARY_HTML = """
<li  class="flex items-baseline border-b border-neutral-200 py-6">
  <a href="/library/llama3.1" class="group w-full space-y-5">
    <div  title="llama3.1" class="flex flex-col">
      <h2 class="truncate text-xl font-medium"><div class="flex space-x-2 items-center">
        <span class="group-hover:underline truncate">llama3.1</span></div></h2>
      <p class="max-w-lg break-words text-neutral-800 text-md">Llama 3.1 is a new
      state-of-the-art model from Meta &amp; available in 8B, 70B and 405B sizes.</p>
    </div>
    <div class="flex flex-col space-y-2"><div class="flex flex-wrap space-x-2">
      <span  class="inline-flex items-center rounded-md bg-indigo-50 px-2 py-0.5">tools</span>
      <span  class="inline-flex items-center rounded-md bg-[#ddf4ff] px-2 py-0.5">8b</span>
      <span  class="inline-flex items-center rounded-md bg-[#ddf4ff] px-2 py-0.5">70b</span>
      <span  class="inline-flex items-center rounded-md bg-[#ddf4ff] px-2 py-0.5">405b</span>
    </div>
    <p class="my-4 flex space-x-5 text-[13px]">
      <span class="flex items-center"><svg></svg>
        <span >119.2M</span><span class="hidden sm:flex">&nbsp;Pulls</span></span>
      <span class="flex items-center"><svg></svg>
        <span >93</span><span class="hidden sm:flex">&nbsp;Tags</span></span>
      <span class="flex items-center" title="Nov 30, 2024 10:34 PM UTC"><svg></svg></span>
    </p></div></a>
</li>
<li  class="flex items-baseline border-b border-neutral-200 py-6">
  <a href="/library/nomic-embed-text" class="group w-full space-y-5">
    <div  title="nomic-embed-text" class="flex flex-col">
      <span class="group-hover:underline truncate">nomic-embed-text</span>
      <p class="max-w-lg break-words text-neutral-800 text-md">A high-performing
      open embedding model.</p></div>
    <div class="flex flex-wrap space-x-2">
      <span  class="inline-flex items-center rounded-md bg-indigo-50">embedding</span></div>
    <p class="my-4 flex space-x-5">
      <span class="flex items-center"><span >84.6M</span>
      <span class="hidden sm:flex">&nbsp;Pulls</span></span></p>
  </a>
</li>
"""


class ScrapeTests(unittest.TestCase):
    def setUp(self):
        self.rows = L.parse_library(LIBRARY_HTML)

    def test_every_card_on_the_page_is_read(self):
        self.assertEqual([r["name"] for r in self.rows], ["llama3.1", "nomic-embed-text"])

    def test_the_description_survives_the_markup(self):
        self.assertEqual(
            self.rows[0]["description"],
            "Llama 3.1 is a new state-of-the-art model from Meta & available in 8B, "
            "70B and 405B sizes.",
        )

    def test_size_badges_become_variants_and_capability_badges_do_not(self):
        self.assertEqual(self.rows[0]["variants"], ["8b", "70b", "405b"])
        self.assertEqual(self.rows[0]["capabilities"], ["tools"])

    def test_the_smallest_variant_is_what_a_machine_has_to_hold(self):
        self.assertEqual(self.rows[0]["min_params"], 8_000_000_000)
        self.assertEqual(self.rows[0]["params"], 405_000_000_000)

    def test_pull_and_tag_counts_are_told_apart(self):
        self.assertEqual(self.rows[0]["downloads"], 119_200_000)
        self.assertEqual(self.rows[0]["tag_count"], 93)

    def test_the_update_date_is_read_from_the_tooltip(self):
        self.assertEqual(self.rows[0]["updated"], "Nov 30, 2024 10:34 PM UTC")

    def test_a_card_with_no_size_badges_still_parses(self):
        self.assertEqual(self.rows[1]["variants"], [])
        self.assertEqual(self.rows[1]["downloads"], 84_600_000)

    def test_markup_that_moved_parses_to_nothing_rather_than_to_nonsense(self):
        self.assertEqual(L.parse_library("<div>Ollama redesigned the page</div>"), [])


class NumberTests(unittest.TestCase):
    def test_rendered_counts_are_read_back(self):
        self.assertEqual(L.parse_count("119.2M"), 119_200_000)
        self.assertEqual(L.parse_count("1,204"), 1204)
        self.assertEqual(L.parse_count("2.1B"), 2_100_000_000)

    def test_something_that_is_not_a_count_is_zero_rather_than_a_crash(self):
        self.assertEqual(L.parse_count("Pulls"), 0)
        self.assertEqual(L.parse_count(None), 0)

    def test_parameter_sizes_are_read_in_either_case(self):
        self.assertEqual(L.parse_params("8b"), 8_000_000_000)
        self.assertEqual(L.parse_params("27.3B"), 27_300_000_000)
        self.assertEqual(L.parse_params("494M"), 494_000_000)

    def test_a_capability_word_is_not_a_size(self):
        for word in ("tools", "vision", "thinking", "embedding"):
            self.assertEqual(L.parse_params(word), 0, word)


class QuantTests(unittest.TestCase):
    # Captured from unsloth/Qwen3.8-27B-GGUF, which carries every awkward case
    # a Hugging Face repository has: a split quant in a directory, a vision
    # projector, calibration data and a speculative-decoding head.
    SIBLINGS = [
        {"rfilename": "BF16/Qwen3.8-27B-BF16-00001-of-00002.gguf", "size": 49 * GIB},
        {"rfilename": "BF16/Qwen3.8-27B-BF16-00002-of-00002.gguf", "size": 5 * GIB},
        {"rfilename": "Qwen3.8-27B-Q4_K_M.gguf", "size": 16 * GIB},
        {"rfilename": "Qwen3.8-27B-Q8_0.gguf", "size": 29 * GIB},
        {"rfilename": "MTP/mtp-Qwen3.8-27B-Q4_0.gguf", "size": 1 * GIB},
        {"rfilename": "mmproj-F16.gguf", "size": 1 * GIB},
        {"rfilename": "imatrix_unsloth.gguf", "size": 1},
        {"rfilename": "README.md", "size": 900},
    ]

    def setUp(self):
        self.quants = L.parse_quants(self.SIBLINGS)

    def test_only_runnable_weights_are_offered(self):
        self.assertEqual([q["label"] for q in self.quants], ["Q4_K_M", "Q8_0", "BF16"])

    def test_a_split_quant_is_one_choice_of_its_total_size(self):
        split = next(q for q in self.quants if q["label"] == "BF16")
        self.assertEqual(split["parts"], 2)
        self.assertEqual(split["weights_bytes"], 54 * GIB)

    def test_a_projector_is_not_offered_as_a_model_but_is_reported(self):
        self.assertNotIn("F16", [q["label"] for q in self.quants])
        self.assertTrue(L.has_projector(self.SIBLINGS))

    def test_a_repository_with_no_projector_says_so(self):
        self.assertFalse(L.has_projector([{"rfilename": "model-Q4_K_M.gguf", "size": 1}]))

    def test_choices_are_ordered_smallest_first(self):
        sizes = [q["weights_bytes"] for q in self.quants]
        self.assertEqual(sizes, sorted(sizes))

    def test_quantization_is_found_in_the_file_name_or_its_directory(self):
        self.assertEqual(L._quant_label("Qwen_Qwen3-8B-Q4_K_M.gguf"), "Q4_K_M")
        self.assertEqual(L._quant_label("Qwen_Qwen3-8B-bf16.gguf"), "bf16")
        self.assertEqual(L._quant_label("Q6_K/model-00001-of-00002.gguf"), "Q6_K")

    def test_a_file_with_no_quantization_in_its_name_is_skipped(self):
        self.assertEqual(L._quant_label("model.gguf"), "")


class CursorTests(unittest.TestCase):
    HEADER = ('<https://huggingface.co/api/models?filter=gguf&cursor=eyJhIjoxfQ>; rel="next"')

    def test_the_cursor_is_read_from_the_link_header(self):
        self.assertEqual(L._next_cursor(self.HEADER), "eyJhIjoxfQ")

    def test_the_last_page_has_no_cursor(self):
        self.assertIsNone(L._next_cursor(None))
        self.assertIsNone(L._next_cursor('<https://x/y>; rel="prev"'))

    def test_only_the_cursor_is_kept_so_the_header_cannot_redirect_us(self):
        # The next request is rebuilt against HF_API, so a Link header pointing
        # somewhere else takes its cursor along and nothing more.
        hostile = '<https://elsewhere.example/collect?cursor=abc123>; rel="next"'
        self.assertEqual(L._next_cursor(hostile), "abc123")


class WarningTests(unittest.TestCase):
    """What Aether can honestly say before a byte has been downloaded."""

    def warn(self, **row):
        base = {"id": "someone/Model-GGUF", "gguf": {"total": 8e9, "context_length": 32768,
                                                     "chat_template": "{%- if tools %}{%- endif %}"}}
        return L.normalize_hf({**base, **row}, installed=set(), budget=24 * GIB)["warnings"]

    def test_a_healthy_repository_warns_about_nothing(self):
        self.assertEqual(self.warn(), [])

    def test_a_gated_repository_says_it_needs_access(self):
        self.assertTrue(any("Gated" in w for w in self.warn(gated=True)))

    def test_a_template_without_tools_says_agentic_will_not_work(self):
        warnings = self.warn(gguf={"total": 8e9, "chat_template": "{{ messages }}"})
        self.assertTrue(any("cannot run Agentic" in w for w in warnings))

    def test_a_missing_template_says_replies_may_come_out_raw(self):
        self.assertTrue(any("raw markers" in w for w in self.warn(gguf={"total": 8e9})))

    def test_a_vision_model_says_its_eyes_are_left_behind(self):
        warnings = self.warn(pipeline_tag="image-text-to-text")
        self.assertTrue(any("text-only" in w for w in warnings))

    def test_a_short_window_is_called_out(self):
        warnings = self.warn(gguf={"total": 8e9, "context_length": 4096,
                                   "chat_template": "{%- if tools %}"})
        self.assertTrue(any("4K context" in w for w in warnings))

    def test_an_unindexed_repository_admits_it_knows_nothing(self):
        self.assertTrue(any("unknown until it is installed" in w for w in self.warn(gguf={})))

    def test_a_model_too_big_for_the_machine_is_marked_rather_than_hidden(self):
        entry = L.normalize_hf({"id": "a/B", "gguf": {"total": 70e9}}, installed=set(), budget=24 * GIB)
        self.assertFalse(entry["fits"])

    def test_an_ollama_model_without_the_tools_badge_warns_the_same_way(self):
        row = {"name": "codellama", "capabilities": [], "min_params": 7e9, "variants": ["7b"]}
        entry = L.normalize_ollama(row, installed=set(), budget=24 * GIB)
        self.assertTrue(any("cannot run Agentic" in w for w in entry["warnings"]))
        self.assertFalse(entry["capabilities"]["tools"])


class InstalledTests(unittest.TestCase):
    INSTALLED = {"qwen3:8b", "hf.co/bartowski/Qwen_Qwen3-8B-GGUF:Q4_K_M"}

    def test_an_ollama_family_is_installed_when_any_of_its_tags_is(self):
        row = {"name": "qwen3", "capabilities": ["tools"], "min_params": 8e9, "variants": ["8b"]}
        self.assertTrue(L.normalize_ollama(row, installed=self.INSTALLED, budget=0)["installed"])

    def test_a_family_that_merely_starts_the_same_way_is_not_installed(self):
        row = {"name": "qwen3-coder", "capabilities": ["tools"], "min_params": 30e9, "variants": ["30b"]}
        self.assertFalse(L.normalize_ollama(row, installed=self.INSTALLED, budget=0)["installed"])

    def test_a_hugging_face_repo_is_installed_whatever_the_case(self):
        row = {"id": "bartowski/qwen_qwen3-8b-gguf", "gguf": {"total": 8e9}}
        self.assertTrue(L.normalize_hf(row, installed=self.INSTALLED, budget=0)["installed"])


class CrossLibraryTests(unittest.TestCase):
    """The same weights under two names must not be downloaded twice."""

    # Qwen3.8 27B, pulled from Ollama. Architecture and parameter count are
    # what /api/show read out of the GGUF header.
    REGISTRY = {
        "qwen3.8:27b": {"id": "qwen3.8:27b", "family": "qwen35", "params": 27_320_697_856},
        "llama3.2:3b": {"id": "llama3.2:3b", "family": "llama", "params": 3_212_749_824},
    }

    def hf(self, repo, params, arch="qwen35"):
        return L.normalize_hf(
            {"id": repo, "gguf": {"total": params, "architecture": arch,
                                  "chat_template": "{%- if tools %}"}},
            installed=set(self.REGISTRY), budget=24 * GIB,
            index=L.installed_index(self.REGISTRY),
        )

    def ollama(self, name, variants):
        return L.normalize_ollama(
            {"name": name, "capabilities": ["tools"], "variants": variants,
             "min_params": L.parse_params(variants[0]),
             "params": L.parse_params(variants[-1])},
            installed=set(self.REGISTRY), budget=24 * GIB,
            index=L.installed_index(self.REGISTRY),
        )

    def test_the_warning_does_not_claim_a_fine_tune_is_the_same_weights(self):
        # Uncensored and abliterated repacks share their base model's
        # architecture and parameter count but are not the same file.
        text = L.same_weights_warning("qwen3.8:27b")
        self.assertIn("Same size and architecture", text)
        self.assertIn("rather than a fine-tune", text)

    def test_a_model_installed_from_ollama_is_recognised_on_hugging_face(self):
        entry = self.hf("unsloth/Qwen3.8-27B-GGUF", 27_320_697_856)
        self.assertEqual(entry["elsewhere"], "qwen3.8:27b")
        self.assertTrue(any("which you already have" in w for w in entry["warnings"]))

    def test_it_is_recognised_even_when_the_repository_name_looks_nothing_like_it(self):
        # A repacked repo keeps the architecture and the size, not the name.
        entry = self.hf("TheBloke/Some-Merge-v3-GGUF", 27_320_697_856)
        self.assertEqual(entry["elsewhere"], "qwen3.8:27b")

    def test_a_different_size_of_the_same_family_is_a_different_model(self):
        self.assertEqual(self.hf("unsloth/Qwen3.8-9B-GGUF", 9_000_000_000)["elsewhere"], "")

    def test_a_different_architecture_at_the_same_size_is_not_a_match(self):
        entry = self.hf("mistralai/Large-GGUF", 27_320_697_856, arch="mistral")
        self.assertEqual(entry["elsewhere"], "")

    def test_a_hugging_face_model_is_recognised_when_browsing_ollama(self):
        registry = {"hf.co/unsloth/Qwen3.8-27B-GGUF:Q4_K_M": {
            "family": "qwen35", "params": 27_320_697_856}}
        entry = L.normalize_ollama(
            {"name": "qwen3.8", "capabilities": ["tools"], "variants": ["27b"],
             "min_params": 27e9, "params": 27e9},
            installed=set(registry), budget=24 * GIB, index=L.installed_index(registry),
        )
        self.assertEqual(entry["elsewhere"], "hf.co/unsloth/Qwen3.8-27B-GGUF:Q4_K_M")

    def test_a_name_that_runs_on_into_another_digit_is_a_different_model(self):
        # qwen3 is not qwen3.8, however much of it reads the same.
        self.assertFalse(L._names_match("qwen3", "unsloth/Qwen3.8-27B-GGUF"))
        self.assertTrue(L._names_match("qwen3.8", "unsloth/Qwen3.8-27B-GGUF"))

    def test_a_name_that_runs_on_into_another_word_is_a_different_model(self):
        # qwen3 and qwen3-coder are both 30B mixtures of experts, so size and
        # architecture alone would call them the same model. The name is what
        # tells them apart.
        self.assertFalse(L._names_match("qwen3", "qwen3-coder"))
        self.assertFalse(L._names_match("mistral", "mistral-small-24b"))

    def test_a_separator_into_a_parameter_size_is_still_the_same_model(self):
        self.assertTrue(L._names_match("qwen3", "qwen3-30b-a3b"))
        self.assertTrue(L._names_match("qwen3-coder", "qwen3-coder-30b"))
        self.assertTrue(L._names_match("llama3.2", "llama3.2"))

    def test_a_coder_variant_is_not_confused_with_its_base_family(self):
        registry = {"qwen3-coder:30b": {"family": "qwen3moe", "params": 30_532_122_624}}
        entry = L.normalize_ollama(
            {"name": "qwen3", "capabilities": ["tools"], "variants": ["30b"],
             "min_params": 30e9, "params": 30e9},
            installed=set(registry), budget=24 * GIB, index=L.installed_index(registry))
        self.assertEqual(entry["elsewhere"], "")

    def test_an_exact_tag_match_is_installed_rather_than_merely_recognised(self):
        registry = {"hf.co/unsloth/Qwen3.8-27B-GGUF:Q4_K_M": {
            "family": "qwen35", "params": 27_320_697_856}}
        entry = L.normalize_hf(
            {"id": "unsloth/Qwen3.8-27B-GGUF", "gguf": {"total": 27_320_697_856,
                                                        "architecture": "qwen35"}},
            installed=set(registry), budget=24 * GIB, index=L.installed_index(registry),
        )
        self.assertTrue(entry["installed"])
        self.assertEqual(entry["elsewhere"], "")


class InstalledVariantTests(unittest.TestCase):
    """Which version is installed, not merely that one is."""

    def test_an_ollama_family_names_the_sizes_that_are_here(self):
        entry = L.normalize_ollama(
            {"name": "llama3.2", "capabilities": ["tools"], "variants": ["1b", "3b"],
             "min_params": 1e9, "params": 3e9},
            installed={"llama3.2:3b", "llama3.1:8b"}, budget=24 * GIB)
        self.assertEqual(entry["installed_variants"], ["3b"])

    def test_more_than_one_version_of_the_same_model_is_listed(self):
        entry = L.normalize_ollama(
            {"name": "qwen3", "capabilities": ["tools"], "variants": ["8b", "14b"],
             "min_params": 8e9, "params": 14e9},
            installed={"qwen3:8b", "qwen3:14b"}, budget=24 * GIB)
        self.assertEqual(entry["installed_variants"], ["14b", "8b"])

    def test_a_hugging_face_repository_names_the_quantization_that_is_here(self):
        entry = L.normalize_hf(
            {"id": "bartowski/Qwen_Qwen3-8B-GGUF", "gguf": {"total": 8e9}},
            installed={"hf.co/bartowski/Qwen_Qwen3-8B-GGUF:Q4_K_M"}, budget=24 * GIB)
        self.assertEqual(entry["installed_variants"], ["Q4_K_M"])

    def test_nothing_installed_names_nothing(self):
        entry = L.normalize_ollama(
            {"name": "gemma3", "capabilities": ["tools"], "variants": ["4b"],
             "min_params": 4e9, "params": 4e9},
            installed={"qwen3:8b"}, budget=24 * GIB)
        self.assertEqual(entry["installed_variants"], [])
        self.assertFalse(entry["installed"])


class RecommendedVariantTests(unittest.IsolatedAsyncioTestCase):
    QUANTS = [
        {"rfilename": "m-Q4_K_M.gguf", "size": 16 * GIB},
        {"rfilename": "m-Q6_K.gguf", "size": 22 * GIB},
        {"rfilename": "m-Q8_0.gguf", "size": 29 * GIB},
        {"rfilename": "m-IQ2_S.gguf", "size": 8 * GIB},
    ]

    async def variants(self, budget):
        with unittest.mock.patch.object(
            L, "hf_repo_detail",
            unittest.mock.AsyncMock(return_value={"siblings": self.QUANTS, "gguf": {}})
        ):
            return await L.variants("huggingface", "a/B-GGUF", budget=budget)

    async def test_the_best_version_is_the_largest_one_that_fits(self):
        found = await self.variants(24 * GIB)
        self.assertEqual(found["variants"][0]["label"], "Q6_K")
        self.assertTrue(found["variants"][0]["recommended"])

    async def test_the_recommendation_leads_the_list(self):
        found = await self.variants(24 * GIB)
        self.assertEqual([v["recommended"] for v in found["variants"]].count(True), 1)
        self.assertIs(found["variants"][0]["recommended"], True)

    async def test_a_smaller_card_is_recommended_a_smaller_quantization(self):
        found = await self.variants(12 * GIB)
        self.assertEqual(found["variants"][0]["label"], "IQ2_S")

    async def test_a_machine_that_can_hold_none_of_them_is_recommended_none(self):
        found = await self.variants(2 * GIB)
        self.assertFalse(any(v["recommended"] for v in found["variants"]))


class DescriptionTests(unittest.TestCase):
    def test_a_quantization_repo_says_what_it_was_made_from(self):
        text = L._hf_description(
            {"id": "unsloth/Qwen3.8-27B-GGUF", "cardData": {"base_model": "Qwen/Qwen3.8-27B",
                                                            "license": "apache-2.0"}},
            {"context_length": 262144})
        self.assertIn("Quantized from Qwen/Qwen3.8-27B", text)
        self.assertIn("256K context", text)
        self.assertIn("apache-2.0", text)

    def test_a_base_model_list_is_read_as_its_first_entry(self):
        text = L._hf_description({"id": "a/B", "cardData": {"base_model": ["Qwen/Qwen3-8B"]}}, {})
        self.assertIn("Quantized from Qwen/Qwen3-8B", text)

    def test_a_repository_that_is_its_own_base_model_falls_back_to_the_header(self):
        text = L._hf_description({"id": "Qwen/Qwen3-8B-GGUF",
                                  "cardData": {"base_model": "Qwen/Qwen3-8B"}},
                                 {"architecture": "qwen3"})
        self.assertIn("qwen3 weights in GGUF", text)

    def test_a_bare_repository_still_describes_itself(self):
        self.assertEqual(L._hf_description({"id": "a/B"}, {"architecture": "llama"}),
                         "llama weights in GGUF")


class BandTests(unittest.TestCase):
    def test_every_size_lands_in_exactly_one_band(self):
        for params, band in ((1e9, "tiny"), (8e9, "small"), (27e9, "medium"), (405e9, "large")):
            self.assertEqual(L._band_of(int(params)), band, params)

    def test_the_bands_leave_no_gap(self):
        edges = sorted(low for low, _ in L.PARAM_BANDS.values())
        for low in edges:
            self.assertEqual(sum(1 for a, b in L.PARAM_BANDS.values()
                                 if a <= low and (b is None or low < b)), 1, low)


class BrowseTests(unittest.TestCase):
    ROWS = [
        {"name": "big", "description": "a large one", "capabilities": ["tools"],
         "variants": ["70b"], "params": 70e9, "min_params": 70e9, "downloads": 900,
         "tag_count": 3, "updated": "Nov 30, 2024 10:34 PM UTC"},
        {"name": "small", "description": "a small one", "capabilities": ["tools"],
         "variants": ["3b"], "params": 3e9, "min_params": 3e9, "downloads": 5000,
         "tag_count": 3, "updated": "Jul 2, 2025 6:09 AM UTC"},
        {"name": "embedder", "description": "vectors only", "capabilities": ["embedding"],
         "variants": [], "params": 0, "min_params": 0, "downloads": 9_000_000,
         "tag_count": 1, "updated": ""},
    ]

    def browse(self, **kwargs):
        with unittest.mock.patch.object(L, "ollama_library",
                                        unittest.mock.AsyncMock(return_value=self.ROWS)):
            return asyncio.run(L.browse("ollama", budget=24 * GIB, **kwargs))

    def test_embedding_models_never_reach_a_chat_picker(self):
        self.assertNotIn("embedder", [e["id"] for e in self.browse()["items"]])

    def test_popular_means_most_pulled(self):
        self.assertEqual([e["id"] for e in self.browse()["items"]], ["small", "big"])

    def test_newest_means_most_recently_updated(self):
        self.assertEqual([e["id"] for e in self.browse(sort="newest")["items"]], ["small", "big"])

    def test_search_reads_the_description_as_well_as_the_name(self):
        self.assertEqual([e["id"] for e in self.browse(query="large")["items"]], ["big"])

    def test_every_word_of_a_search_has_to_match(self):
        self.assertEqual(self.browse(query="large tiny")["items"], [])

    def test_a_band_filter_keeps_only_that_band(self):
        self.assertEqual([e["id"] for e in self.browse(band="tiny")["items"]], ["small"])

    def test_only_what_fits_drops_what_the_machine_cannot_hold(self):
        self.assertEqual([e["id"] for e in self.browse(fits_only=True)["items"]], ["small"])

    def test_paging_walks_the_list_once_and_then_stops(self):
        first = self.browse(limit=1)
        self.assertEqual([e["id"] for e in first["items"]], ["small"])
        second = self.browse(limit=1, cursor=first["next"])
        self.assertEqual([e["id"] for e in second["items"]], ["big"])
        self.assertIsNone(second["next"])

    def test_an_unreachable_library_empties_the_list_rather_than_raising(self):
        with unittest.mock.patch.object(L, "ollama_library",
                                        unittest.mock.AsyncMock(return_value=[])):
            self.assertEqual(asyncio.run(L.browse("ollama"))["items"], [])


@unittest.skipUnless(os.environ.get("AETHER_LIVE_TESTS") == "1", "set AETHER_LIVE_TESTS=1")
class LiveTests(unittest.TestCase):
    def test_ollamas_library_page_still_parses(self):
        rows = asyncio.run(L.ollama_library(refresh=True))
        self.assertGreater(len(rows), 50)
        self.assertTrue(all(r["name"] for r in rows))
        self.assertTrue(any(r["downloads"] for r in rows))

    def test_hugging_face_still_returns_the_gguf_header_inline(self):
        rows, cursor = asyncio.run(L.hf_search("qwen3", "popular", limit=5))
        self.assertTrue(rows)
        self.assertTrue(any((r.get("gguf") or {}).get("total") for r in rows))
        self.assertTrue(cursor)

    def test_a_hugging_face_repository_lists_installable_quantizations(self):
        found = asyncio.run(L.variants("huggingface", "bartowski/Qwen_Qwen3-8B-GGUF"))
        self.assertTrue(found["variants"])
        self.assertIn("Q4_K_M", [v["label"] for v in found["variants"]])
        self.assertTrue(all(v["weights_bytes"] > 0 for v in found["variants"]))

    def test_an_ollama_model_lists_its_tags_with_real_sizes(self):
        found = asyncio.run(L.variants("ollama", "qwen3"))
        self.assertGreater(len(found["variants"]), 5)
        self.assertTrue(any(v["weights_bytes"] > 0 for v in found["variants"]))

    def test_a_hugging_face_tag_sizes_the_same_as_the_pull(self):
        size = L.hf_tag_size("hf.co/bartowski/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M")
        self.assertGreater(size, 300 * 1024**2)

    def test_a_quantization_that_does_not_exist_sizes_to_zero(self):
        self.assertEqual(L.hf_tag_size("hf.co/bartowski/Qwen_Qwen3-8B-GGUF:NOT_A_QUANT"), 0)
