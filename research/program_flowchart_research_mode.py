"""
research-mode as a program flowchart.

This is the 440-line research-mode.json fixture rewritten as a Python program.
Same behavior, same actions emitted — just expressed as code instead of a graph.

Flow:
  1. Set up workspace files
  2. Decompose the research question into sub-questions and search queries
  3. Loop: search → read → extract → gap analysis → decide to continue
  4. Synthesize a comprehensive cited report
  5. Print summary and finish
"""

import json
import os

from flowcoder import runtime


def setup_workspace():
    os.makedirs("/tmp/fc-research", exist_ok=True)
    with open("/tmp/fc-research/knowledge.md", "w") as f:
        f.write("# Research Knowledge Base\n")
    with open("/tmp/fc-research/sources.json", "w") as f:
        f.write("[]")
    with open("/tmp/fc-research/next_queries.txt", "w") as f:
        f.write("")


def main():
    args = runtime.start()
    question = args["research-question"]

    # --- SETUP WORKSPACE ---
    setup_workspace()

    # --- DECOMPOSE QUERY ---
    plan = runtime.query(
        f"""You are a research planner. Your job is to analyze a research question and create a thorough plan.

RESEARCH QUESTION: {question}

Tasks:
1. Break the question into 3-7 sub-questions that together would comprehensively answer the main question
2. For each sub-question, generate 1-2 search queries that would find relevant information
3. Write a concise research plan summarizing what you need to find

Write your research plan to /tmp/fc-research/plan.md
Write the initial search queries (one per line) to /tmp/fc-research/next_queries.txt

Then return the structured output below.""",
        output_schema={
            "type": "object",
            "properties": {
                "num_sub_questions": {"type": "integer"},
                "plan_summary": {"type": "string"},
            },
            "required": ["num_sub_questions", "plan_summary"],
        },
    )

    # --- ITERATIVE SEARCH LOOP ---
    should_continue = True
    iteration = 0

    while should_continue and iteration < 15:
        runtime.clear()

        result = runtime.query(
            f"""You are a research assistant conducting one iteration of a deep web research process.

ORIGINAL RESEARCH QUESTION: {question}

First, read these files to understand the current state of the research:
1. /tmp/fc-research/plan.md — the research plan with sub-questions
2. /tmp/fc-research/knowledge.md — all findings accumulated so far
3. /tmp/fc-research/next_queries.txt — the search queries to use this iteration
4. /tmp/fc-research/sources.json — sources found so far

Then perform these steps:
1. SEARCH: Use the web search tool to search for each query in next_queries.txt. Examine the results carefully.
2. READ: Pick the 2-4 most promising results based on domain authority (.gov, .edu, major publications), relevance to unanswered sub-questions, and recency. Fetch and read the full content of those pages.
3. EXTRACT: For each page, extract facts relevant to the research question. Note the URL and key claims.
4. EVALUATE: Assess source credibility. Flag any contradictions with existing knowledge.
5. GAP ANALYSIS: Compare what you now know against the research plan. What sub-questions are still unanswered? What needs more evidence?
6. DECIDE: Based on your gap analysis:
   - If significant gaps remain and new searches would help → should_continue = true, and write new targeted search queries to /tmp/fc-research/next_queries.txt
   - If all sub-questions are well-answered with multiple corroborating sources → should_continue = false

IMPORTANT: Append your new findings to /tmp/fc-research/knowledge.md (do NOT overwrite — append with a clear iteration header).
Update /tmp/fc-research/sources.json by reading the current array and appending new source entries.

Then return the structured output.""",
            output_schema={
                "type": "object",
                "properties": {
                    "should_continue": {"type": "boolean"},
                    "sources_found_this_iteration": {"type": "integer"},
                    "gaps_remaining": {"type": "string"},
                    "iteration_summary": {"type": "string"},
                },
                "required": [
                    "should_continue",
                    "sources_found_this_iteration",
                    "gaps_remaining",
                    "iteration_summary",
                ],
            },
        )

        print(f"Iteration {iteration} complete: {result['iteration_summary']}")
        print(f"Gaps remaining: {result['gaps_remaining']}")

        should_continue = result["should_continue"]
        iteration += 1

    # --- SYNTHESIZE REPORT ---
    runtime.clear()

    report = runtime.query(
        f"""You are a research synthesizer. Your job is to produce a comprehensive, well-cited research report.

ORIGINAL RESEARCH QUESTION: {question}

Read these files containing all the research gathered:
1. /tmp/fc-research/plan.md — the original research plan
2. /tmp/fc-research/knowledge.md — ALL accumulated findings from every search iteration
3. /tmp/fc-research/sources.json — all sources with URLs, titles, and key facts

Now write a comprehensive research report following these guidelines:
- ORGANIZE by theme or sub-question, NOT by source or iteration
- CROSS-REFERENCE facts across multiple sources, noting where they agree or disagree
- Use INLINE CITATIONS like [1], [2] etc. linked to the sources list
- Include a SUMMARY section at the top (3-5 key takeaways)
- Include a SOURCES section at the end with numbered references and URLs
- Note any CAVEATS about source reliability or gaps in the research
- Target 2000-4000 words — be comprehensive but avoid repetition

Write the complete report to /tmp/fc-research/report.md

Then return the structured output.""",
        output_schema={
            "type": "object",
            "properties": {
                "report_word_count": {"type": "integer"},
                "num_sources_cited": {"type": "integer"},
                "confidence_level": {"type": "string"},
            },
            "required": [
                "report_word_count",
                "num_sources_cited",
                "confidence_level",
            ],
        },
    )

    # --- DONE ---
    print("=== RESEARCH COMPLETE ===")
    print(f"Report: /tmp/fc-research/report.md")
    print(f"Sources: {report['num_sources_cited']} cited")
    print(f"Confidence: {report['confidence_level']}")
    print(f"Word count: ~{report['report_word_count']}")

    runtime.finish(report)


main()
