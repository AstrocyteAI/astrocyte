"""LLM prompts for the FinanceBench answerer and judge.

Modelled on Mafin2.5's evaluation setup (VectifyAI/Mafin2.5-FinanceBench),
adapted to Astrocyte's retrieval output shape.
"""

ANSWERER_SYSTEM = """\
You are a financial analyst answering questions about SEC filings.
Answer the question using only the provided document context.
Be concise and precise. For numerical answers include units (e.g. "$394.3 billion", "12.5%").
If the information is not present in the context, respond with exactly: Not found in context.
Do not include explanations, caveats, or anything beyond the direct answer.
"""

ANSWERER_USER = """\
Document context:
{context}

Question: {question}

Answer:"""

# Judge prompt — binary CORRECT / INCORRECT verdict.
# A correct answer must convey the same financial fact as the ground truth:
# same number, same unit, same meaning. Minor formatting differences are OK.
JUDGE_SYSTEM = """\
You are evaluating a financial question-answering system.
Given a question, the correct answer, and a model answer, decide if the model answer is correct.

Rules:
- The model answer must convey the same financial fact as the correct answer.
- Numbers must be equivalent (e.g. "$394.3 billion" == "$394,300 million" == "394.3B").
- Small rounding differences (±0.1) are acceptable for multi-decimal values.
- "Not found in context" is always INCORRECT.
- Extra explanatory text around a correct fact is still CORRECT.

Respond with exactly one word: CORRECT or INCORRECT.
"""

JUDGE_USER = """\
Question: {question}
Correct answer: {ground_truth}
Model answer: {model_answer}

Verdict:"""
