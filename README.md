# sentinel
Sentinel Core is a self-hosted compliance checker. You give it a document (an insurance application, a contract, a patient file) and your company's reference policy (the "vault"). It returns a rule-by-rule verdict with cited evidence. It does the review a compliance analyst does by hand today, and the sensitive data never leaves compute you control

## How it works

Ingest. You upload a document, PDF or text, through a small web UI or the FastAPI endpoint. The engine extracts the relevant facts.
Compare. A Nemotron 3 Nano 30B-A3B model runs on vLLM on a dedicated, single-tenant Nebius GPU. It checks the document against each rule in the vault. No data goes to a shared or third-party LLM API.
Verify. When the vault can't settle a rule, for example whether a regulatory threshold is still in force, the engine makes one scoped Tavily search. It is not open browsing. It confirms the fact against a public source and cites the URL. Only that query leaves the perimeter, never the document.
Decide. Each rule gets one of three verdicts: cumple (passes), no_cumple (fails), or revisar (needs human review), each with the specific evidence behind it. When evidence is insufficient, it returns revisar instead of guessing. That is what makes the output auditable.

### Example from a life-insurance underwriting file

It flags a "non-smoker" declaration contradicted by a positive cotinine test (no_cumple).
It flags a $3M sum insured that exceeds 15× declared income without justification (revisar).
It checks online for a $2M MXN regulatory threshold, finds no clear source, and marks it revisar rather than assuming the threshold still applies.

## Why it matters

Insurance, legal, and healthcare teams all need AI-assisted document review. None of them can send client data, privileged material, or patient health information to a multi-tenant vendor API, because of NAIC and Colorado SB21-169 rules, the EU AI Act, zero-data-retention demands from law firms, and HIPAA's business associate agreement requirements. Sentinel Core gives them that review with full data isolation. The same engine works across sectors by swapping the vault: insurance and a second domain (legal or health) run on identical code.

It generalizes Sentinel PLD, the anti-money-laundering product, which already runs this extract, compare, and verdict pattern fully self-hosted in active pilots.
