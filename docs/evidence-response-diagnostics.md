# Evidence answer response diagnostics

`retrieve@13 / compose@36` is an explicit candidate. The defaults remain
`retrieve@9 / compose@34`. Version 12 keeps its frozen contract and messages.

The governed answer call uses a structured stream, including when no public
text delta is requested. Its JSON-object transport framing requires a JSON
object but does not itself send the Pydantic schema. Version 13 includes the
complete nested response schema in its frozen messages and input budget. It
does not relax output, citation, source or calendar validation, add a model
call, or change the existing deadline and retry policy. Semantic coverage is
still a fallible model judgment.

An HTTP 200 and a successful physical attempt can precede JSON or schema
rejection. Evidence-answer generation failures now request a separate
`answer-model-failure-evidence-answer-v1` immutable diagnostic. It binds the
input and model request, records finite failure categories, and preserves
reported usage and unknown observations. It is neither an answer result nor
factual evidence. Exception messages and response text are excluded from this
diagnostic; public traces contain only its safe classification.

The isolated heldout runner can explicitly enable `--capture-private-responses`.
The default is off. This stores bounded content, finish reasons and reported
usage under the runner's existing output directory, in `private-responses`.
Existing captures are not overwritten. The observer does not pre-read the
stream, request another response, or change physical attempt accounting.
Request credentials, headers and reasoning text are not captured. Capture
state does not establish provider or application success; a truncated prefix
cannot be treated as a complete response for replay.

Response files can contain private material and must remain in the isolated
evaluation output. They must not be committed or copied into public logs.
The capture is diagnostic only and does not authorize model calls or bypass
the runner's source, physical-budget or output-isolation checks.

The response capture observes completion SDK objects, not HTTP response bytes.
The SDK can synthesize terminal and usage fields. On the recognized, owned
LiteLLM/OpenAI streaming path, a lazy observer before LiteLLM conversion now
requires an actual provider finish reason and uses only provider-reported
usage. A terminal response without usage can still deliver an answer with
unknown usage. An early EOF uses the existing bounded transport retry policy;
every resulting physical attempt remains charged to its attempt ledger. Usage
reported before a missing terminal is retained; real zero counters stay zero.
This shared transport correction also applies to fresh calls using older policies;
it does not change their frozen messages or add calls when a response completes
normally. It does not establish that an SDK-consumed `[DONE]` marker was present.

Synthetic SDK and service controls can verify protocol handling, direct
delivery, fallback, accounting and diagnostics. They do not identify the cause
of an earlier response that was not retained, or establish real-model quality
and latency. Those require a separately authorized server evaluation.
