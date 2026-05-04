# Risk

Every live-action path must define explicit limits, invalidation logic, and a draft fallback.
Always rank a null-state lane before arming any job.
Do not arm a path unless market quality checks and the risk block both pass.
If an action path cannot satisfy the risk block, the compiler should return `draft` or `null`, not `armed`.
