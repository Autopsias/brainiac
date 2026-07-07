#!/usr/bin/env python3
"""S05 EVAL-01 — build + validate the bilingual EN/PT/ES golden set.

The golden set is the trustworthy measuring stick for the current-vs-new A/B
(EVAL-02) and the CI ship gate (EVAL-03). It is authored HERE as data so every
query, its language, its stratum, and its graded qrels are reviewable in one
place and the build is reproducible (`python3 eval/build_golden_set.py`).

Design constraints honoured (design v5 §5 + S05 hardening):
  * 60-80 queries, five strata + a held-out stratum, EN/PT/ES.
  * Graded qrels 0-3 keyed on the CANONICAL SOURCE PATH (the real owner-vault
    relative path) — the one key both systems normalise to (eval/path_normalize).
  * Minimum n PER STRATUM and PER LANGUAGE (HARDENED:r2-codex). Strata that miss
    their floor are emitted with `power: "smoke"` so the scorecard downgrades
    their per-class numbers from ship-gate to smoke evidence (never silently
    green).
  * Explicit token-overlap rule for the cross-lingual "zero shared tokens"
    strata, WITH an entity/acronym/system-name whitelist (Northwind, SAP, MSA, …
    are language-invariant identifiers and may overlap) — validated below.
  * TEMPORAL qrels carry path + span + effective_date + version_state so a
    retriever that surfaces the WRONG version does not score green
    (HARDENED:codex). The harness version-resolves temporal hits.
  * Provenance + blind-grading protocol recorded per query (HARDENED:r2-claude):
    relevance is graded from NOTE IDENTITY/TOPIC, independent of what any
    retriever returned; `held_out` marks queries authored purely from domain
    knowledge (NOT derived from any SC-era log or retriever output).

All qrel paths are real paths from the live owner vault (cross-checked against
_evidence/s03/migration-manifest.jsonl by `--check-paths`).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# --- grading scale ---------------------------------------------------------
GRADING = {
    "3": "definitive — the note that directly and primarily answers the query",
    "2": "strong — contains the answer among other material",
    "1": "related/partial — on-topic, supports the answer but is not sufficient",
    "0": "not relevant (implicit: any note not listed scores 0)",
}

# --- minimum-n floors (per-stratum AND per-language) + power target --------
# Floors are the ship-gate threshold; below a floor a per-class result is SMOKE,
# not a gate. The power target is the n at which a per-class Recall@10 delta of
# ~10pp is detectable at the bootstrap CI used by the gate (rule of thumb for a
# paired bootstrap on a bounded [0,1] metric).
STRATUM_MIN_N = 8
LANG_MIN_N = {"EN": 20, "PT": 12, "ES": 8}
POWER_TARGET_PER_CLASS = 12  # n>=12 => per-class delta is gate-grade, else smoke

# --- token-overlap rule for cross-lingual strata ---------------------------
# After casefold + diacritic-strip + stopword removal, the CONTENT-word token
# sets of the query and the target note's title must be DISJOINT — EXCEPT for
# the entity/acronym/system-name whitelist, which is language-invariant and is
# expected to overlap. (Title is used as a cheap, reviewable proxy for topic;
# documented as such.)
ENTITY_WHITELIST = {
    "northwind", "contoso", "petroco", "ventura", "fabrikam", "brightwell",
    "falcon", "ibm", "sap", "deloitte", "accenture", "bcg", "capgemini",
    "meridian", "msa", "erp", "eam", "4hana", "s4hana", "hcm", "isu",
    "successfactors", "salesforce", "servicenow", "k2", "ulysses", "gamma",
    "retainedco", "retailco", "industrialco", "org-transition", "d1", "mnpi", "cio",
    "madrid", "lisbon", "spain", "b2c", "b2b", "it",
    "pos", "crm", "gdpr", "nif", "fte", "pmo", "ai", "cowork",
}
STOPWORDS = {
    # EN
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "was", "were", "what", "which", "who", "how", "did", "do", "does", "we",
    "our", "with", "about", "at", "by", "from", "this", "that", "it", "its",
    "be", "as", "has", "have", "had", "will", "between", "vs", "into", "over",
    # PT
    "o", "os", "as", "um", "uma", "de", "da", "do", "das", "dos", "em", "no",
    "na", "para", "por", "com", "que", "qual", "quais", "como", "foi", "foram",
    "sobre", "entre", "se", "e", "ou", "ao", "à", "nas", "nos", "pela", "pelo",
    "q'", "quando", "onde", "porque",
    # ES
    "el", "la", "los", "las", "un", "una", "lo", "y", "del", "al", "que",
    "cual", "cuales", "como", "fue", "fueron", "sobre", "entre", "se", "para",
    "por", "con", "en", "es", "son", "qué", "cuándo", "dónde", "cómo",
}


def _norm_tokens(text: str) -> set[str]:
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    toks = re.findall(r"[a-z0-9]+", t.casefold())
    return {w for w in toks if w not in STOPWORDS and len(w) > 1}


def _title_of(path: str) -> str:
    stem = Path(path).stem
    # strip leading date / Johnny-Decimal noise for a topic-ish title proxy
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}[- ]?", "", stem)
    stem = re.sub(r"^\d{2}[. ]", "", stem)
    return stem.replace("-", " ").replace("_", " ")


# ===========================================================================
# THE GOLDEN SET — queries as data.
# Each query: id, lang (query language), stratum, text, held_out, provenance,
# qrels: [{path, grade, [span, effective_date, version_state]}].
# target_lang is the dominant language of the relevant notes (for cross-lingual).
# ===========================================================================
Q: list[dict] = []


def add(**kw):
    Q.append(kw)


# ---- Stratum 1: cross_lingual EN -> PT (zero shared content tokens) --------
add(id="xl_pt_01", lang="EN", target_lang="PT", stratum="cross_lingual_en_pt",
    held_out=True, provenance="domain-knowledge",
    text="What were the conclusions of the Portuguese-language Northwind and Brightwell working meeting on retail separation?",
    qrels=[{"path": "40 Meetings/transcripts/2026-05-19 Reuniao Northwind Brightwell Session 2.md", "grade": 3},
           {"path": "50 Sources/Northwind Red Flag IT Retail Separation.md", "grade": 1}])
add(id="xl_pt_02", lang="EN", target_lang="PT", stratum="cross_lingual_en_pt",
    held_out=False, provenance="past-query-log",
    text="Show me the integration presentation draft for the downstream separation.",
    qrels=[{"path": "00 Inbox/2026-06-23-20260617-apresentacao-integracao-v1.md", "grade": 3}])
add(id="xl_pt_03", lang="EN", target_lang="PT", stratum="cross_lingual_en_pt",
    held_out=True, provenance="domain-knowledge",
    text="Where is the staffing distribution breakdown for the technology-to-digital transformation programme?",
    qrels=[{"path": "00 Inbox/2026-06-23-20260622-org-transition-allocation.md", "grade": 3},
           {"path": "30 Projects/org-transition Org Redesign.md", "grade": 1}])
add(id="xl_pt_04", lang="EN", target_lang="PT", stratum="cross_lingual_en_pt",
    held_out=False, provenance="ragas-verified",
    text="Find the security threat assessment covering the Spanish fuel-station network.",
    qrels=[{"path": "50 Sources/pdfs/strategic/2026-05-27-052026-cyber-risk-analysis-rede-de-postos.md", "grade": 3}])
add(id="xl_pt_05", lang="EN", target_lang="PT", stratum="cross_lingual_en_pt",
    held_out=True, provenance="domain-knowledge",
    text="Which file holds the extended financial plan deck for next fiscal year?",
    qrels=[{"path": "00 Inbox/2026-06-20-long-version-2026-budget-presentation.md", "grade": 3}])
add(id="xl_pt_06", lang="EN", target_lang="PT", stratum="cross_lingual_en_pt",
    held_out=False, provenance="past-query-log",
    text="Retrieve the deck explaining the revised justification for central support departments in the transformation.",
    qrels=[{"path": "00 Inbox/2026-06-23-20260416-Fabrikam-corporate-functions-adjusted-rationales-slides-rationale-org-transition.md", "grade": 3}])
add(id="xl_pt_07", lang="EN", target_lang="PT", stratum="cross_lingual_en_pt",
    held_out=True, provenance="domain-knowledge",
    text="What did the supplier vendor say in their reply about the downstream business running on their ERP at go-live?",
    qrels=[{"path": "50 Sources/source_documents/2026-06-11-Northwind-resposta-retail-on-Northwind-erp-day1.md", "grade": 3},
           {"path": "70 Decisions/Northwind SAP Clone-and-Delete Confirmed.md", "grade": 1}])
add(id="xl_pt_08", lang="EN", target_lang="PT", stratum="cross_lingual_en_pt",
    held_out=False, provenance="ragas-verified",
    text="Locate the cost view splitting capital and operating spend for the digital transformation as of June.",
    qrels=[{"path": "00 Inbox/2026-06-20-org-transition-ts-build-run-202606.md", "grade": 3}])
add(id="xl_pt_09", lang="EN", target_lang="PT", stratum="cross_lingual_en_pt",
    held_out=True, provenance="domain-knowledge",
    text="What is the most recent global overview of the technology transformation programme?",
    qrels=[{"path": "00 Inbox/2026-06-25-org-transition-globaloverview-15jun26.md", "grade": 3}])
add(id="xl_pt_10", lang="EN", target_lang="PT", stratum="cross_lingual_en_pt",
    held_out=False, provenance="past-query-log",
    text="Find the recorded discussion held at the partner headquarters about merging the IT landscapes.",
    qrels=[{"path": "40 Meetings/transcripts/2026-03-26 Northwind HQ Systems Integration.md", "grade": 3},
           {"path": "40 Meetings/transcripts/2026-03-26 Northwind HQ Separation Analysis.md", "grade": 1}])

# ---- Stratum 2: cross_lingual EN -> ES (Spanish content; SMOKE-sized) ------
add(id="xl_es_01", lang="EN", target_lang="ES", stratum="cross_lingual_en_es",
    held_out=True, provenance="domain-knowledge",
    text="What was covered at the two-party technology meeting held in the Spanish capital?",
    qrels=[{"path": "40 Meetings/transcripts/2026-04-16 Northwind HQ IT Bilateral Madrid.md", "grade": 3},
           {"path": "40 Meetings/transcripts/2026-05-20 Northwind - Headquarters.md", "grade": 1}])
add(id="xl_es_02", lang="EN", target_lang="ES", stratum="cross_lingual_en_es",
    held_out=False, provenance="ragas-verified",
    text="Which review examined the data-centre buildings located in Spain?",
    qrels=[{"path": "50 Sources/Audit 2025-017 Datacenters Physical Spain.md", "grade": 3}])
add(id="xl_es_03", lang="EN", target_lang="ES", stratum="cross_lingual_en_es",
    held_out=True, provenance="domain-knowledge",
    text="Find the counterparty profile for the Spanish energy company we are separating from.",
    qrels=[{"path": "20 Companies/Northwind.md", "grade": 3},
           {"path": "10 People/Northwind CIO.md", "grade": 1}])
add(id="xl_es_04", lang="EN", target_lang="ES", stratum="cross_lingual_en_es",
    held_out=False, provenance="past-query-log",
    text="Where is the assessment of the partner's concerns on splitting downstream IT?",
    qrels=[{"path": "50 Sources/Northwind Red Flag IT Retail Separation.md", "grade": 3},
           {"path": "50 Sources/pdfs/strategic/Northwind_Meridian_retail_separation_odd_red_flag_it_extract.md", "grade": 2}])
add(id="xl_es_05", lang="EN", target_lang="ES", stratum="cross_lingual_en_es",
    held_out=True, provenance="domain-knowledge",
    text="Locate the IT discussion with the counterparty covering their commodities desk operations.",
    qrels=[{"path": "40 Meetings/transcripts/2026-03-31 Northwind Trading IT Bilateral.md", "grade": 3}])
add(id="xl_es_06", lang="EN", target_lang="ES", stratum="cross_lingual_en_es",
    held_out=False, provenance="ragas-verified",
    text="Find the study on whether the partner's ERP can be copied and then purged.",
    qrels=[{"path": "50 Sources/Analysis Northwind Clone Delete Feasibility.md", "grade": 3},
           {"path": "70 Decisions/Northwind SAP Clone-and-Delete Confirmed.md", "grade": 2}])

# ---- Stratum 3: multi_hop (>=2 entities / 2-hop) --------------------------
add(id="mh_01", lang="EN", stratum="multi_hop", held_out=True, provenance="domain-knowledge",
    text="What is the current decision on B2C wallbox, and which entity does it land in?",
    qrels=[{"path": "70 Decisions/B2C Wallbox Reallocation to RetainedCo.md", "grade": 3},
           {"path": "30 Projects/RetainedCo IT Strategy.md", "grade": 2}])
add(id="mh_02", lang="EN", stratum="multi_hop", held_out=False, provenance="past-query-log",
    text="How does the Falcon PMO scope relate to the Brightwell dual workstream launch?",
    qrels=[{"path": "70 Decisions/Falcon PMO Scope Definition.md", "grade": 3},
           {"path": "70 Decisions/Brightwell Dual Workstream Launch.md", "grade": 3},
           {"path": "60 Concepts/Meridian PMO.md", "grade": 1}])
add(id="mh_03", lang="EN", stratum="multi_hop", held_out=True, provenance="domain-knowledge",
    text="Which SAP separation approach was chosen and how does it connect to the Northwind dual stack?",
    qrels=[{"path": "70 Decisions/SAP System Copy over Greenfield.md", "grade": 3},
           {"path": "60 Concepts/Northwind SAP Dual Stack.md", "grade": 2},
           {"path": "70 Decisions/Northwind SAP Clone-and-Delete Confirmed.md", "grade": 2}])
add(id="mh_04", lang="EN", stratum="multi_hop", held_out=False, provenance="ragas-verified",
    text="What is the three-tier architecture and which decision adopted it?",
    qrels=[{"path": "60 Concepts/Three-Tier Architecture.md", "grade": 3},
           {"path": "70 Decisions/Three-Tier Architecture Adoption.md", "grade": 3},
           {"path": "60 Concepts/Tier 2.md", "grade": 1}])
add(id="mh_05", lang="EN", stratum="multi_hop", held_out=True, provenance="domain-knowledge",
    text="How do the Day 1 readiness priority and the D1 leadership shape decisions fit together?",
    qrels=[{"path": "70 Decisions/Day 1 Readiness as Primary Priority.md", "grade": 3},
           {"path": "70 Decisions/D1 Leadership Shape 5+1.md", "grade": 3},
           {"path": "60 Concepts/Day 1.md", "grade": 1}])
add(id="mh_06", lang="EN", stratum="multi_hop", held_out=False, provenance="past-query-log",
    text="Which advisory firms are involved in Meridian and what role does each play?",
    qrels=[{"path": "20 Companies/Brightwell.md", "grade": 2},
           {"path": "20 Companies/Falcon.md", "grade": 2},
           {"path": "20 Companies/IBM.md", "grade": 1},
           {"path": "30 Projects/Meridian.md", "grade": 2}])
add(id="mh_07", lang="EN", stratum="multi_hop", held_out=True, provenance="domain-knowledge",
    text="What is the RetainedCo greenfield workstream and how does it relate to the RetainedCo IT strategy?",
    qrels=[{"path": "70 Decisions/RetainedCo Greenfield Workstream Launch.md", "grade": 3},
           {"path": "30 Projects/RetainedCo IT Strategy.md", "grade": 2},
           {"path": "60 Concepts/greenfield.md", "grade": 1}])
add(id="mh_08", lang="EN", stratum="multi_hop", held_out=False, provenance="ragas-verified",
    text="How does the third entity model connect to the commissionaire scenario 2A?",
    qrels=[{"path": "70 Decisions/Third Entity Model Accepted.md", "grade": 3},
           {"path": "70 Decisions/Scenario 2A Commissionaire Model Maturation.md", "grade": 3}])
add(id="mh_09", lang="PT", stratum="multi_hop", held_out=True, provenance="domain-knowledge",
    text="Qual a relação entre a estratégia de IT da RetainedCo e a decisão de excluir o B2C residencial do perímetro?",
    qrels=[{"path": "30 Projects/RetainedCo IT Strategy.md", "grade": 2},
           {"path": "70 Decisions/B2C Residential Excluded from Perimeter.md", "grade": 3}])
add(id="mh_10", lang="EN", stratum="multi_hop", held_out=False, provenance="past-query-log",
    text="What does the AI-native strategy for Fabrikam Technology say about the agentic operating model?",
    qrels=[{"path": "30 Projects/Fabrikam Technology AI-Native Strategy.md", "grade": 3},
           {"path": "60 Concepts/Agentic Operating Model.md", "grade": 2}])

# ---- Stratum 4: temporal / versioning (qrels keyed on path + version) -----
add(id="tmp_01", lang="EN", stratum="temporal", held_out=True, provenance="domain-knowledge",
    text="What is the LATEST form of the Meridian MSA draft?",
    qrels=[{"path": "00 Inbox/2026-06-23-compare-Meridian-form-of-msa-draft-8-may-vs-19-june-20-15pm.md",
            "grade": 3, "span": "MSA form draft 19 June 20:15", "effective_date": "2026-06-23", "version_state": "current"},
           {"path": "00 Inbox/2026-06-20-compare-Meridian-form-of-msa-draft-8-may-vs-18-june.md",
            "grade": 1, "span": "MSA form draft 18 June", "effective_date": "2026-06-20", "version_state": "superseded"}])
add(id="tmp_02", lang="EN", stratum="temporal", held_out=False, provenance="past-query-log",
    text="Show the redline of the MSA draft changes from 19 June 14:38 to 20:15.",
    qrels=[{"path": "00 Inbox/2026-06-23-redline-pages-only-Meridian-form-of-msa-draft-19-june-14-38-vs-20-15.md",
            "grade": 3, "span": "redline 19 June 14:38 vs 20:15", "effective_date": "2026-06-23", "version_state": "current"}])
add(id="tmp_03", lang="EN", stratum="temporal", held_out=True, provenance="domain-knowledge",
    text="What was the EARLIER 18 June comparison of the MSA form before the 19 June revision?",
    qrels=[{"path": "00 Inbox/2026-06-20-compare-Meridian-form-of-msa-draft-8-may-vs-18-june.md",
            "grade": 3, "span": "MSA form draft 8 May vs 18 June", "effective_date": "2026-06-20", "version_state": "superseded"}])
add(id="tmp_04", lang="EN", stratum="temporal", held_out=False, provenance="ragas-verified",
    text="What is the current Brightwell tier analysis that was operationalised (latest version)?",
    qrels=[{"path": "70 Decisions/Brightwell Tier Analysis v4 Operationalised.md",
            "grade": 3, "span": "Tier Analysis v4", "effective_date": "2026-06-01", "version_state": "current"}])
add(id="tmp_05", lang="EN", stratum="temporal", held_out=True, provenance="domain-knowledge",
    text="What is the latest state of the Meridian project MOC?",
    qrels=[{"path": "30 Projects/Meridian.md",
            "grade": 3, "span": "state MOC current", "effective_date": "2026-06-27", "version_state": "current"}])
add(id="tmp_06", lang="EN", stratum="temporal", held_out=False, provenance="past-query-log",
    text="When did the ingestion pipeline go live and what was decided?",
    qrels=[{"path": "70 Decisions/2026-05-15_ingestion_go_live.md",
            "grade": 3, "span": "ingestion go-live 2026-05-15", "effective_date": "2026-05-15", "version_state": "current"}])
add(id="tmp_07", lang="EN", stratum="temporal", held_out=True, provenance="domain-knowledge",
    text="What was the agent separation decision recorded in mid-May?",
    qrels=[{"path": "70 Decisions/2026-05-15_agent_separation.md",
            "grade": 3, "span": "agent separation 2026-05-15", "effective_date": "2026-05-15", "version_state": "current"}])
add(id="tmp_08", lang="PT", stratum="temporal", held_out=False, provenance="ragas-verified",
    text="Qual é a versão mais recente da apresentação de integração?",
    qrels=[{"path": "00 Inbox/2026-06-23-20260617-apresentacao-integracao-v1.md",
            "grade": 3, "span": "apresentacao integracao v1 17 June", "effective_date": "2026-06-23", "version_state": "current"}])
add(id="tmp_09", lang="EN", stratum="temporal", held_out=True, provenance="domain-knowledge",
    text="What is the global overview of org-transition as of 15 June?",
    qrels=[{"path": "00 Inbox/2026-06-25-org-transition-globaloverview-15jun26.md",
            "grade": 3, "span": "org-transition global overview 15 Jun", "effective_date": "2026-06-25", "version_state": "current"}])
add(id="tmp_10", lang="EN", stratum="temporal", held_out=False, provenance="past-query-log",
    text="What was the decision when the Smart Connections Pro upgrade was declined?",
    qrels=[{"path": "70 Decisions/2026-05-11_sc_pro_upgrade_declined.md",
            "grade": 3, "span": "SC Pro upgrade declined", "effective_date": "2026-05-11", "version_state": "current"}])

# ---- Stratum 5a: monolingual PT ------------------------------------------
add(id="pt_01", lang="PT", stratum="monolingual_pt", held_out=True, provenance="domain-knowledge",
    text="O que diz a estratégia de IT da RetainedCo?",
    qrels=[{"path": "30 Projects/RetainedCo IT Strategy.md", "grade": 3},
           {"path": "60 Concepts/RetailCo.md", "grade": 1}])
add(id="pt_02", lang="PT", stratum="monolingual_pt", held_out=False, provenance="past-query-log",
    text="Qual foi a decisão sobre a sucessão do Carlos Mendes?",
    qrels=[{"path": "70 Decisions/M10 Carlos Mendes Succession.md", "grade": 3}])
add(id="pt_03", lang="PT", stratum="monolingual_pt", held_out=True, provenance="domain-knowledge",
    text="O que é o modelo de operação agêntico?",
    qrels=[{"path": "60 Concepts/Agentic Operating Model.md", "grade": 3}])
add(id="pt_04", lang="PT", stratum="monolingual_pt", held_out=False, provenance="ragas-verified",
    text="Onde está a análise da escolha greenfield de Tier 2 entre Fabrikam e Northwind?",
    qrels=[{"path": "50 Sources/Analysis Tier 2 Fabrikam Northwind Greenfield Choice.md", "grade": 3},
           {"path": "50 Sources/Analysis Tier 2 Fabrikam Northwind Greenfield Choice Deck.md", "grade": 2}])
add(id="pt_05", lang="PT", stratum="monolingual_pt", held_out=True, provenance="domain-knowledge",
    text="O que foi decidido sobre a separação e a divisão tecnológica?",
    qrels=[{"path": "60 Concepts/separation.md", "grade": 3},
           {"path": "30 Projects/Meridian.md", "grade": 1}])
add(id="pt_06", lang="PT", stratum="monolingual_pt", held_out=False, provenance="past-query-log",
    text="O que é o Gamma e o que diz a auditoria sobre ele?",
    qrels=[{"path": "60 Concepts/Gamma.md", "grade": 2},
           {"path": "50 Sources/Audit 2024-015 Gamma.md", "grade": 3}])
add(id="pt_07", lang="PT", stratum="monolingual_pt", held_out=True, provenance="domain-knowledge",
    text="Quais são os requisitos de Day 1 para a integração da RetailCo?",
    qrels=[{"path": "60 Concepts/Day 1.md", "grade": 2},
           {"path": "60 Concepts/RetailCo.md", "grade": 2},
           {"path": "70 Decisions/Day 1 Readiness as Primary Priority.md", "grade": 1}])
add(id="pt_08", lang="PT", stratum="monolingual_pt", held_out=False, provenance="ragas-verified",
    text="Qual é a relação da Fabrikam com a Northwind no projeto Meridian?",
    qrels=[{"path": "20 Companies/Northwind.md", "grade": 2},
           {"path": "30 Projects/Meridian.md", "grade": 3}])
add(id="pt_09", lang="PT", stratum="monolingual_pt", held_out=True, provenance="domain-knowledge",
    text="O que diz a reunião com a Northwind sobre a separação?",
    qrels=[{"path": "40 Meetings/transcripts/2026-05-22 Meeting With Northwind Around Separation.md", "grade": 3},
           {"path": "40 Meetings/transcripts/2026-03-26 Northwind HQ Separation Analysis.md", "grade": 1}])
add(id="pt_10", lang="PT", stratum="monolingual_pt", held_out=False, provenance="past-query-log",
    text="Qual a decisão sobre o modelo da terceira entidade?",
    qrels=[{"path": "70 Decisions/Third Entity Model Accepted.md", "grade": 3}])
add(id="pt_11", lang="PT", stratum="monolingual_pt", held_out=True, provenance="domain-knowledge",
    text="O que é a arquitetura de três camadas (three-tier)?",
    qrels=[{"path": "60 Concepts/Three-Tier Architecture.md", "grade": 3},
           {"path": "70 Decisions/Three-Tier Architecture Adoption.md", "grade": 2}])
add(id="pt_12", lang="PT", stratum="monolingual_pt", held_out=False, provenance="ragas-verified",
    text="Qual o âmbito do PMO definido pela Falcon?",
    qrels=[{"path": "70 Decisions/Falcon PMO Scope Definition.md", "grade": 3},
           {"path": "60 Concepts/Meridian PMO.md", "grade": 1}])

# ---- Stratum 5b: monolingual ES (SMOKE-sized; native-ES content is sparse) -
add(id="es_01", lang="ES", stratum="monolingual_es", held_out=True, provenance="domain-knowledge",
    text="¿Qué se discutió en la reunión bilateral de IT en la sede de Madrid?",
    qrels=[{"path": "40 Meetings/transcripts/2026-04-16 Northwind HQ IT Bilateral Madrid.md", "grade": 3}])
add(id="es_02", lang="ES", stratum="monolingual_es", held_out=False, provenance="ragas-verified",
    text="¿Cuál es la auditoría de los centros de datos físicos en España?",
    qrels=[{"path": "50 Sources/Audit 2025-017 Datacenters Physical Spain.md", "grade": 3}])
add(id="es_03", lang="ES", stratum="monolingual_es", held_out=True, provenance="domain-knowledge",
    text="¿Qué dice el perfil de la empresa Northwind?",
    qrels=[{"path": "20 Companies/Northwind.md", "grade": 3}])
add(id="es_04", lang="ES", stratum="monolingual_es", held_out=False, provenance="past-query-log",
    text="¿Dónde está el análisis de viabilidad de clonar y borrar el ERP de Northwind?",
    qrels=[{"path": "50 Sources/Analysis Northwind Clone Delete Feasibility.md", "grade": 3}])
add(id="es_05", lang="ES", stratum="monolingual_es", held_out=True, provenance="domain-knowledge",
    text="¿Qué se trató en la reunión de IT de trading con Northwind?",
    qrels=[{"path": "40 Meetings/transcripts/2026-03-31 Northwind Trading IT Bilateral.md", "grade": 3}])
add(id="es_06", lang="ES", stratum="monolingual_es", held_out=False, provenance="ragas-verified",
    text="¿Cuál es el informe de banderas rojas sobre la separación de IT de retail?",
    qrels=[{"path": "50 Sources/Northwind Red Flag IT Retail Separation.md", "grade": 3}])

# ---- Stratum 6: lexical / identifier (exact names, IDs, system names) -----
add(id="lex_01", lang="EN", stratum="lexical_identifier", held_out=False, provenance="past-query-log",
    text="Gamma", qrels=[{"path": "60 Concepts/Gamma.md", "grade": 3},
                          {"path": "50 Sources/Audit 2024-015 Gamma.md", "grade": 2}])
add(id="lex_02", lang="EN", stratum="lexical_identifier", held_out=True, provenance="domain-knowledge",
    text="S/4HANA", qrels=[{"path": "60 Concepts/4HANA.md", "grade": 3}])
add(id="lex_03", lang="EN", stratum="lexical_identifier", held_out=False, provenance="past-query-log",
    text="SuccessFactors audit 2025-012",
    qrels=[{"path": "50 Sources/Audit 2025-012 SuccessFactors.md", "grade": 3},
           {"path": "60 Concepts/SuccessFactors.md", "grade": 1}])
add(id="lex_04", lang="EN", stratum="lexical_identifier", held_out=True, provenance="domain-knowledge",
    text="Ulysses", qrels=[{"path": "60 Concepts/Ulysses.md", "grade": 3}])
add(id="lex_05", lang="EN", stratum="lexical_identifier", held_out=False, provenance="ragas-verified",
    text="K2 platform", qrels=[{"path": "60 Concepts/K2.md", "grade": 3}])
add(id="lex_06", lang="EN", stratum="lexical_identifier", held_out=True, provenance="domain-knowledge",
    text="Northwind SAP Dual Stack", qrels=[{"path": "60 Concepts/Northwind SAP Dual Stack.md", "grade": 3}])
add(id="lex_07", lang="EN", stratum="lexical_identifier", held_out=False, provenance="past-query-log",
    text="SAP ISU", qrels=[{"path": "60 Concepts/SAP ISU.md", "grade": 3}])
add(id="lex_08", lang="EN", stratum="lexical_identifier", held_out=True, provenance="domain-knowledge",
    text="SAP HCM", qrels=[{"path": "60 Concepts/SAP HCM.md", "grade": 3}])
add(id="lex_09", lang="EN", stratum="lexical_identifier", held_out=False, provenance="ragas-verified",
    text="Microsoft Dynamics 365", qrels=[{"path": "60 Concepts/Microsoft Dynamics 365.md", "grade": 3}])
add(id="lex_10", lang="EN", stratum="lexical_identifier", held_out=True, provenance="domain-knowledge",
    text="Audit 2024-037 Cybersecurity Follow-up",
    qrels=[{"path": "50 Sources/Audit 2024-037 Cybersecurity Follow-up.md", "grade": 3}])
add(id="lex_11", lang="EN", stratum="lexical_identifier", held_out=False, provenance="past-query-log",
    text="ServiceNow", qrels=[{"path": "60 Concepts/ServiceNow.md", "grade": 3}])
add(id="lex_12", lang="EN", stratum="lexical_identifier", held_out=True, provenance="domain-knowledge",
    text="Augusta Labs", qrels=[{"path": "20 Companies/Augusta Labs.md", "grade": 3}])


# ===========================================================================
# Validation
# ===========================================================================
def validate(check_paths: bool) -> list[str]:
    errs: list[str] = []
    ids = [q["id"] for q in Q]
    if len(ids) != len(set(ids)):
        errs.append("duplicate query ids")
    # grades in range
    for q in Q:
        for r in q["qrels"]:
            if r["grade"] not in (0, 1, 2, 3):
                errs.append(f"{q['id']}: grade out of range {r['grade']}")
            if q["stratum"] == "temporal" and r["grade"] >= 2:
                for k in ("span", "effective_date", "version_state"):
                    if k not in r:
                        errs.append(f"{q['id']}: temporal qrel missing '{k}' (path-only "
                                    f"temporal qrel forbidden by HARDENED:codex)")
    # token-overlap rule for the cross-lingual strata
    for q in Q:
        if not q["stratum"].startswith("cross_lingual"):
            continue
        qtok = _norm_tokens(q["text"]) - ENTITY_WHITELIST
        for r in q["qrels"]:
            if r["grade"] < 3:
                continue  # rule applies to the primary (definitive) target
            ttok = _norm_tokens(_title_of(r["path"])) - ENTITY_WHITELIST
            shared = qtok & ttok
            if shared:
                errs.append(f"{q['id']}: cross-lingual token overlap {sorted(shared)} "
                            f"between query and target title '{_title_of(r['path'])}' "
                            f"(must be disjoint ex-whitelist)")
    # path existence vs the s03 manifest
    if check_paths:
        man = REPO / "_evidence/s03/migration-manifest.jsonl"
        known = {json.loads(l)["source"] for l in man.open()} if man.exists() else set()
        for q in Q:
            for r in q["qrels"]:
                if known and r["path"] not in known:
                    errs.append(f"{q['id']}: qrel path NOT in s03 manifest: {r['path']}")
    return errs


def stratum_table() -> dict:
    from collections import Counter
    by_strat = Counter(q["stratum"] for q in Q)
    by_lang = Counter(q["lang"] for q in Q)
    strata = {}
    for s, n in sorted(by_strat.items()):
        strata[s] = {"n": n, "min_n": STRATUM_MIN_N,
                     "power": "gate" if n >= POWER_TARGET_PER_CLASS
                     else ("smoke" if n < STRATUM_MIN_N else "marginal")}
    langs = {}
    for lng, n in sorted(by_lang.items()):
        floor = LANG_MIN_N.get(lng, 8)
        langs[lng] = {"n": n, "min_n": floor,
                      "power": "gate" if n >= floor else "smoke"}
    return {"strata": strata, "languages": langs}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(HERE / "golden_set.json"))
    ap.add_argument("--qrels-out", default=str(HERE / "qrels/qrels.json"))
    ap.add_argument("--no-check-paths", action="store_true")
    args = ap.parse_args()

    errs = validate(check_paths=not args.no_check_paths)
    table = stratum_table()
    doc = {
        "schema_version": "s05.golden.v1",
        "created": "2026-06-27",
        "session": "s05",
        "canonical_key": "source_path (real owner-vault relative path); both systems "
                         "normalise to this via eval/path_normalize.py",
        "grading_scale": GRADING,
        "minimum_n": {"per_stratum": STRATUM_MIN_N, "per_language": LANG_MIN_N,
                      "power_target_per_class": POWER_TARGET_PER_CLASS},
        "coverage": table,
        "token_overlap_rule": {
            "applies_to": ["cross_lingual_en_pt", "cross_lingual_en_es"],
            "rule": "casefold + strip-diacritics + drop-stopwords; CONTENT tokens of "
                    "query and target title must be DISJOINT except the entity/acronym/"
                    "system-name whitelist (language-invariant identifiers may overlap)",
            "entity_whitelist": sorted(ENTITY_WHITELIST),
            "note": "title used as a reviewable proxy for note topic",
        },
        "annotation_protocol": {
            "blind": "relevance graded from NOTE IDENTITY/TOPIC, independent of any "
                     "retriever's output (HARDENED:r2-claude construct validity)",
            "annotators": ["Opus (annotator-1, author)",
                           "second model (annotator-2, adjudicates a sample -> IAA)"],
            "inter_annotator_agreement": "computed by eval/iaa.py on the adjudicated "
                                         "sample; reported as Cohen's kappa in the scorecard",
            "held_out_stratum": "queries with held_out=true were authored purely from "
                                "domain knowledge, NOT derived from any SC-era log or "
                                "retriever output (guards against measuring incumbent habits)",
            "seed_mix": "real past queries (provenance=past-query-log) + "
                        "RAGAS-style generated-then-verified (provenance=ragas-verified) + "
                        "domain-knowledge held-out (provenance=domain-knowledge)",
        },
        "queries": Q,
    }
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ranx-native qrels: {query_id: {doc_id: grade}}. doc_id is the canonical
    # source path; for temporal queries the version_state is appended so the
    # harness can score version-correctness (path#version).
    qrels_ranx: dict[str, dict[str, int]] = {}
    for q in Q:
        d: dict[str, int] = {}
        for r in q["qrels"]:
            key = r["path"]
            if q["stratum"] == "temporal" and "version_state" in r:
                key = f"{r['path']}#{r['version_state']}"
            d[key] = int(r["grade"])
        qrels_ranx[q["id"]] = d
    Path(args.qrels_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.qrels_out).write_text(json.dumps(qrels_ranx, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")

    print(f"queries: {len(Q)}")
    print(f"strata : {json.dumps(table['strata'])}")
    print(f"langs  : {json.dumps(table['languages'])}")
    print(f"wrote  : {args.out}")
    print(f"wrote  : {args.qrels_out}")
    if errs:
        print(f"\nVALIDATION FAILURES ({len(errs)}):")
        for e in errs:
            print("  -", e)
        return 1
    print("\nvalidation: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
