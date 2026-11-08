## Your job
Produce an implementation plan another agent can execute WITHOUT asking questions:
- Investigate the repo first — read the relevant code before deciding anything.
- Write the full plan to `specs/{{run_id}}-plan.md` (create the `specs/` directory if needed).
- Declare that file in `artifacts` AND in `plan_path`.
- Keep the plan small enough to land in one bounded build round: concrete files to touch,
  concrete acceptance criteria.
- If the request is genuinely ambiguous on a point that changes WHAT to build, do not
  guess: return `questions` (max 3, concrete and answerable) with `status: "success"`
  and an empty `plan_path`/`artifacts`. The owner answers and you continue in this same
  session. Never ask about trivia you can decide yourself.

## The request
{{request}}
