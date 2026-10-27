# Coverage: 128 failure routes → what this desk does

Source catalogue: [docs/failure-catalogue.md](docs/failure-catalogue.md) (built from Airwallex docs, Nacha, FBI IC3, Stripe/Adyen idempotency notes, OWASP LLM Top 10).

✅ built and tested · 🟡 partly handled · ⚪ out of scope for this build (reason given)
**95 built and tested · 19 partial · 14 out of scope** (of 128)


| ID | Failure route | | How it is handled | Proof |
|---|---|---|---|---|
| INT-01 | Same invoice ingested via two channels | ✅ | Normalized supplier+invoice key, unique index | test_duplicate_invoice_refused |
| INT-02 | Invoice-number variant bypasses exact dedupe | ✅ | Invoice numbers normalized (case, spaces, dashes) before dedupe | test_duplicate_invoice_refused |
| INT-03 | Reminder or statement treated as a new invoice | ✅ | Same payee + same amount within 30 days needs explicit "separate invoice" confirmation | test_reminder_or_corrected_copy_needs_confirmation |
| INT-04 | Corrected invoice paid alongside the original | ✅ | Same as INT-03 | test_reminder_or_corrected_copy_needs_confirmation |
| INT-05 | Agent mis-extracts or hallucinates invoice fields | 🟡 | Agent never extracts money fields; amounts typed by operator, parsed exactly (no floats) | test_amounts_are_exact |
| INT-05a | Locale decimal confusion | ✅ | Strict amount parser: digits + optional .dd only; "1.250,00" and "1,250" rejected | server.cents |
| INT-05b | Ambiguous currency symbol | ✅ | Single currency (USD) enforced by policy and payee account currency check | test_currency_mismatch_blocked_before_send |
| INT-05c | Minor-unit exponent errors | ✅ | Integer cents internally; exact decimal string to Airwallex | test_amounts_are_exact |
| INT-06 | Indirect prompt injection in invoice or email | ✅ | Supplier text is "untrusted" data; agent has no money tools; bank-change and instruction-to-the-AI wording raise FRAUD_SIGNAL (a heuristic - the real boundary is that code only allows listed actions) | test_supplier_bank_change_request_escalates_even_if_retryable |
| INT-07 | Fake invoice from a non-supplier | 🟡 | Only allow-listed payees can be paid; no automated intake from email in this build |  |
| INT-08 | Spoofed or lookalike sender domain | ⚪ | No email intake in scope (operator opens incidents) |  |
| INT-09 | Overbilling against PO or contract | ⚪ | No PO/contract data in scope |  |
| INT-10 | Paying before due date or before goods are received | ⚪ | No due-date/goods-receipt data in scope |  |
| INT-11 | Data exfiltration triggered by document content | ✅ | Agent evidence excludes bank details, credentials and raw provider payloads | Service.evidence |
| BEN-01 | BEC bank-detail change (vendor impersonation) | ✅ | Agent cannot change payees; bank-change emails escalate; changed details trigger STOP + re-verify + small-payment window | test_bank_details_changed_after_approval_blocks_and_kills, test_changed_payee_small_payments_first |
| BEN-01a | Real vendor mailbox compromised (DMARC passes) | ✅ | Same controls regardless of email authenticity (we never trust email) | test_supplier_bank_change_request_escalates_even_if_retryable |
| BEN-01b | Lookalike domain | ✅ | Same as BEN-01a |  |
| BEN-01c | Deepfake voice or video "confirmation" | 🟡 | Re-verify requires a written note; small-payment window limits loss if the call was faked | test_changed_payee_small_payments_first |
| BEN-02 | Insider redirects a beneficiary | ✅ | Payee edits inside Airwallex detected by details-hash before every first send; desk STOPs | test_bank_details_changed_after_approval_blocks_and_kills; sandbox scenario D |
| BEN-03 | Supplier typo in account or IBAN | ✅ | Bank-detail failures never auto-retry; replacement only after a person reopens with a note | test_bad_details_replacement_only_after_reopen |
| BEN-04 | CoP/VoP name not FULL_MATCH | ⚪ | Confirmation of Payee is UK/EU only; US sandbox has no CoP |  |
| BEN-05 | Beneficiary changed between approval and submission (TOCTOU) | ✅ | Approval stores the payee-details hash it was given; must still match at send | test_approval_bound_to_payee_details_it_saw |
| BEN-06 | Duplicate vendor-master records | 🟡 | One account under two names is refused (BEN-07); duplicate records of the same name allowed | test_mule_account_under_new_name_refused |
| BEN-07 | One bank account shared by several vendors | ✅ | Same account+routing under a different payee name needs an override reason | test_mule_account_under_new_name_refused |
| BEN-08 | Method or currency incompatible with beneficiary | ✅ | Payee account currency must equal payment currency (pre-send check) | test_currency_mismatch_blocked_before_send |
| BEN-09 | Corridor-specific required fields missing | ✅ | Airwallex schema validation; a definite 4xx is recorded as REJECTED and never retried with the same terms | Service.submit |
| BEN-10 | Character-set or length violations | ✅ | References sanitized to [A-Za-z0-9 #./_-], max 40 | test_reference_is_sanitised |
| BEN-11 | Agent bypasses the vendor master via inline beneficiary | ✅ | Transfers only by allow-listed beneficiary_id; no inline beneficiary path exists | adapter.create_transfer |
| BEN-12 | Sanctioned or high-risk jurisdiction payee | ✅ | Payee bank country must be on the allowed list (pre-send) | test_payee_country_outside_allowlist |
| BEN-13 | Stale beneficiary (closed or dormant account) | ✅ | ACCOUNT_CLOSED/DORMANT failures escalate; never auto-replace | test_bad_bank_details_escalate_never_replace |
| APR-01 | Agent fabricates or self-asserts approval | ✅ | Approvals only via operator token; agent has no approval route | server: /api auth; test_agent_cannot_force_disallowed_action |
| APR-02 | Approval replay or rebinding | ✅ | Approvals single-use (unique index) and bound to payee, amount, currency, payee hash | test_approval_is_single_use, test_changed_terms_refused |
| APR-03 | Threshold evasion by splitting | ✅ | 24h total to a payee over the co-sign limit needs a co-signer | test_splitting_under_cosign_limit_caught |
| APR-04 | Rubber-stamp approvals (approval fatigue) | 🟡 | Approval UI shows DB facts (payee, last4, amount, expiry); co-sign over threshold |  |
| APR-05 | Agent summary misleads the approver | ✅ | Approver sees database facts, not agent prose; agent text labelled "model" | web UI |
| APR-06 | Stale approval | ✅ | Approvals expire (default 15 min UI, max 24h) | test_expired_approval_refused |
| APR-07 | Segregation-of-duties failure | ✅ | Co-signer must be a different named operator | test_large_payment_needs_second_person |
| APR-08 | Airwallex approval states mishandled | ✅ | Unknown/approval statuses fail closed as in-flight | test_unknown_provider_status_counts_as_in_flight |
| APR-09 | Urgency or override path abused | ✅ | No override path skips caps; reopen requires a written note kept in the hash-chained log | test_bad_details_replacement_only_after_reopen |
| RES-01 | Concurrent overspend of a budget or allowance | ✅ | BEGIN IMMEDIATE reservations; caps include reserved and uncertain payments | test_concurrent_reservations_respect_daily_cap |
| RES-02 | Insufficient wallet balance | ✅ | Wallet balance checked before first send | test_insufficient_balance_checked_before_sending |
| RES-03 | Reservation leak | ✅ | Stale RESERVED recovered by the reconciler with the same request_id | test_crash_after_reserve_is_recovered_once |
| RES-04 | Premature release | ✅ | Budget released only on a final CANCELLED; UNKNOWN keeps it reserved across restarts/days | test_unknown_survives_restart_and_counts_against_cap |
| RES-05 | FX moved or quote expired | ⚪ | Single currency, no FX in scope |  |
| RES-06 | Fees not budgeted, beneficiary short-paid | ✅ | Fees recorded from amount_payer_pays/fee_amount and counted against caps; while a payment is uncertain the max possible fee is held; a failed transfer's fee stays counted; over-limit alarm | test_fees_count_and_over_limit_alerts, test_unknown_payment_reserves_its_possible_fee, test_cancelled_payment_keeps_its_nonrefundable_fee_in_budget |
| RES-07 | Wrong source wallet or unintended conversion | ✅ | source_currency = transfer_currency = USD enforced |  |
| RES-08 | Account or method limits breached | ✅ | Per-payment, daily and per-payee caps; provider limit errors recorded as REJECTED | test_per_tx_cap, test_per_supplier_cap |
| RES-09 | Agent loop runs away with spend | ✅ | Payments-per-hour limit, attempt cap, agent decision throttle | test_rate_limit, test_replace_loop_capped, test_agent_loop_throttled |
| SUB-01 | Timeout after provider accepted, then retry with a new key | ✅ | Timeout -> UNKNOWN, budget held, reconcile by the same request_id; Airwallex duplicate_request_id adopted | test_timeout_after_submit_stays_reserved_and_blocks_second_payment; sandbox scenario B |
| SUB-02 | Crash between provider success and DB write | ✅ | request_id persisted before the provider call; recovery re-reads by request_id | test_unknown_not_found_resubmits_same_request_id |
| SUB-03 | Same key, different parameters | ✅ | Transfer body built from DB rows only; same request_id always carries the same terms |  |
| SUB-04 | Idempotency key generated at call time | ✅ | Deterministic request_id = UUIDv5(desk, incident, attempt), stored before sending | test_request_id_is_deterministic_per_attempt |
| SUB-05 | Retry after the provider's dedupe window or on a non-succeeded key | ✅ | Our own DB is the dedupe authority (partial unique index), not the provider window | test_database_index_blocks_second_live_payment_even_if_code_is_bypassed |
| SUB-06 | Two workers submit the same payable | ✅ | BEGIN IMMEDIATE + unique indexes; two workers cannot both reserve | test_concurrent_reservations_respect_daily_cap |
| SUB-07 | Expired lease or zombie worker | 🟡 | Same-request_id resubmits are idempotent at Airwallex; no lease table |  |
| SUB-08 | Error classification wrong (unknown treated as failed) | ✅ | Three-way classification: 4xx REJECTED / timeout+5xx UNKNOWN / 2xx CREATED | adapter.Ambiguous, Service.submit |
| SUB-09 | LLM supplies money fields | ✅ | LLM returns only an action enum; money fields come from the database | agent.py, test_llm_choice_outside_allowed_is_downgraded |
| SUB-10 | Wrong tool or wrong action | ✅ | Code computes allowed_actions(); anything else is rejected and logged | test_agent_cannot_force_disallowed_action |
| SUB-11 | `transfer_date` and timezone semantics | ✅ | transfer_date never set (no scheduled-transfer semantics) | adapter.create_transfer |
| SUB-12 | Wrong environment | ✅ | Adapter refuses any base URL except api.sandbox.airwallex.com | adapter.Airwallex.__init__ |
| SUB-13 | Retry storm and rate limiting | ✅ | Reconciler spaced per payment; webhook re-reads throttled to 1 per 5s per payment | Service.on_webhook |
| SUB-14 | Batch partially fails, whole batch resubmitted | ⚪ | No batch payouts in scope |  |
| FLT-01 | Forged webhook | ✅ | HMAC-SHA256(x-timestamp + body) when a secret is set; webhooks never set status, they trigger a re-read | test_forged_webhook_cannot_mark_paid; server webhook |
| FLT-02 | Replayed genuine webhook | ✅ | Timestamp window + event-id dedupe | test_webhook_event_id_dedupe |
| FLT-03 | Duplicate delivery processed twice | ✅ | event id stored once (primary key) | test_webhook_event_id_dedupe, test_duplicate_webhooks_are_harmless |
| FLT-04 | Out-of-order events cause regression | ✅ | Final states never regress; stale reads logged and ignored | test_late_update_after_final_is_ignored |
| FLT-05 | Missed webhooks | ✅ | Background reconciler re-reads anything non-final every 5 min | test_sweeper_rechecks_stale_and_flags_sla |
| FLT-06 | Slow handler triggers redelivery while still running | ✅ | Webhook handler is a fast re-read; duplicates deduped |  |
| FLT-07 | Agent replaces a slow SENT or PROCESSING transfer | ✅ | REPLACE impossible while any payment is non-final | test_retryable_failure_allows_one_replacement |
| FLT-08 | Compliance review or information request | ⚪ | Compliance RFIs not exposed in sandbox; would surface as a non-final status (fails closed) |  |
| FLT-09 | OVERDUE (unfunded scheduled transfer) | ✅ | OVERDUE treated as in-flight; SLA alarm | test_unknown_provider_status_counts_as_in_flight |
| FLT-10 | Supplier claims non-receipt while PAID | ✅ | PAID + "never received" -> ESCALATE with proof-of-payment, never pay again | test_supplier_says_not_received_while_paid |
| FLT-11 | Wrong or ineligible cancel | 🟡 | Desk never cancels transfers; STOP only blocks new sends |  |
| FLT-12 | Clock, timezone or business-day errors | 🟡 | All times stored as UTC epoch; caps by UTC day with in-flight carry-over |  |
| FLT-13 | Agent polling or decision loop | ✅ | Agent throttle per incident | test_agent_loop_throttled |
| TRM-01 | Replace on FAILED before CANCELLED or refund | ✅ | Replace only after a fresh GET shows CANCELLED | test_retryable_failure_allows_one_replacement |
| TRM-02 | Failure type mapped to the wrong action | ✅ | Exhaustive failure_type -> class table; unknown -> UNCLASSIFIED -> escalate | service.RETRYABLE/DETAILS_PROBLEM/DELIBERATE |
| TRM-02a | Bank-detail failures | ✅ | Bank-detail failures escalate; replacement only after reopen-with-note | test_bad_bank_details_escalate_never_replace |
| TRM-02b | Data-quality failures | ✅ | Same as 02a |  |
| TRM-02c | Account status failures | ✅ | Account-status failures escalate |  |
| TRM-02d | Policy or compliance failures | ✅ | TM_SUSPENDED / policy failures escalate |  |
| TRM-02e | Transient provider failures | ✅ | CHANNEL_TIMEOUT / SYSTEM_ERROR / INSUFFICIENT_FUNDS may be replaced (with operator approval) | sandbox scenario A |
| TRM-02f | Intentional stops | ✅ | RECALL/CLIENT_REQUESTED escalate | test_recall_escalates |
| TRM-02g | Payee-side refusal | ✅ | BENEFICIARY_REQUESTED escalates |  |
| TRM-02h | OTHER or unrecognised | ✅ | OTHER and unrecognised messages escalate |  |
| TRM-03 | New status or enum not handled | ✅ | Fail closed: any non-final status counts as in-flight | test_unknown_provider_status_counts_as_in_flight |
| TRM-04 | Two live attempts per payable | ✅ | Partial unique index: one non-final payment per incident | test_database_index_blocks_second_live_payment_even_if_code_is_bypassed |
| TRM-05 | Replacement for the wrong amount | ✅ | Replacement must equal the invoice amount, payee and currency | test_approval_must_match_invoice |
| TRM-06 | Replace loop burning fees | ✅ | Max 3 attempts per invoice | test_replace_loop_capped |
| TRM-07 | Fees and FX delta not booked | 🟡 | Fees recorded per attempt; FX delta out of scope (single currency) |  |
| TRM-08 | Refund in an unexpected currency | ⚪ | Single currency |  |
| TRM-09 | Premature finality (SENT or PAID treated as final) | ✅ | PAID treated as provisional: re-read for 5 days; PAID->FAILED raises an alarm | test_paid_is_rechecked_for_days; sandbox scenario E |
| TRM-10 | Agent under- or over-escalates | ✅ | Code floor: agent can only pick allowed actions; alarms force ESCALATE | test_llm_choice_outside_allowed_is_downgraded |
| TRM-11 | Decision on stale state | ✅ | Agent run re-reads every non-final payment first | test_agent_refreshes_state_before_deciding |
| PST-01 | PAID → FAILED (late reject by clearing or recipient bank) | ✅ | PAID_THEN_CANCELLED anomaly freezes the incident | test_paid_then_returned_freezes_incident |
| PST-02 | Local-rail return after settlement (e.g. ACH) | ✅ | Same handling as PST-01 (sandbox can simulate PAID->FAILED) | sandbox scenario E |
| PST-03 | Duplicate payment discovered after settlement | ✅ | DOUBLE_PAID alarm; close needs a refund note | test_cancelled_then_paid_with_replacement_is_double_paid |
| PST-04 | Payment to a fraudster discovered | 🟡 | Escalation + evidence receipt; recall itself is a human/bank process |  |
| PST-05 | Unmatched supplier refund | ⚪ | Inbound refunds/deposits not matched in this build |  |
| PST-06 | Recall refused or funds gone | ⚪ | Recovery is off-platform |  |
| PST-07 | Shortfall top-up paid twice | ⚪ | No top-ups in scope |  |
| PST-08 | Late event after closure corrupts the new attempt | ✅ | Per-request_id state; final states never regress | test_late_update_after_final_is_ignored |
| REC-01 | Ledger vs wallet balance drift | 🟡 | Balance read before sends; no full ledger-vs-wallet reconciliation |  |
| REC-02 | Orphan transfer at Airwallex (not in our DB) | ✅ | Orphan scan of Airwallex transfers not created by the desk -> STOP | test_orphan_transfer_stops_the_desk; sandbox scenario F |
| REC-03 | Phantom transfer (ours, not at the provider) | ✅ | UNKNOWN/RESERVED not found at Airwallex -> resubmit same request_id | test_unknown_not_found_resubmits_same_request_id |
| REC-04 | ERP or AP status out of sync | ⚪ | No ERP in scope |  |
| REC-05 | Rounding, FX and fee posting errors | ✅ | Integer cents end to end |  |
| REC-06 | Period cut-off errors | 🟡 | UTC-day caps with in-flight carry-over |  |
| REC-07 | Unexplainable agent decision | ✅ | Decision events store the full evidence + hash, allowed actions, provenance | test_decision_log_keeps_evidence |
| REC-08 | Missing or invalid remittance reference | ✅ | Invoice number sent as the bank reference, sanitized | test_reference_is_sanitised |
| MON-01 | API key leaked | ✅ | Orphan scan catches use of the key outside the desk; scoped sandbox key | test_orphan_transfer_stops_the_desk |
| MON-02 | Agent holds an admin key | ✅ | The agent holds no Airwallex key at all; only the adapter does |  |
| MON-03 | Webhook secret rotation breaks verification | 🟡 | Single webhook secret; rotation not automated |  |
| MON-04 | Thresholds leaked via the prompt | 🟡 | Evidence shows remaining budget only; no policy internals in the prompt |  |
| MON-05 | Model or prompt change regression | ✅ | 96 tests (incl. 20 red-team tests + 9 review tests from two independent reviewers) + 6 live sandbox scenarios + a 9-incident model exam (scripts/llm_eval.py) | tests/, scripts/sandbox_e2e.py |
| MON-06 | Tool or MCP supply-chain poisoning | ⚪ | No MCP/tool plugins in the agent |  |
| MON-07 | Agent memory poisoning | ✅ | Agent is stateless per decision (no memory to poison) |  |
| MON-08 | No working kill switch | ✅ | STOP switch checked in the create path, including resubmits | test_kill_switch_blocks, test_stop_blocks_resubmit_of_unknown |
| MON-09 | Unexpected key or webhook changes | 🟡 | Payee edits detected (hash); key/webhook config changes not monitored |  |
| MON-10 | Mis-set limits or config | ✅ | Policy validated at boot | test_unsafe_policy_rejected |
| MON-11 | API version drift | 🟡 | Fails closed on unknown statuses/failure types; API version not pinned |  |
| MON-12 | Sensitive data sent to the LLM | ✅ | No bank details or credentials in agent evidence |  |
| MON-13 | Originator fraud-monitoring obligation unmet | 🟡 | Payee-change controls + orphan scan + caps (Nacha 2026 "false pretenses" monitoring); no formal program |  |
| MON-14 | Intake flood (cost and DoS) | ✅ | New-incident rate limit | test_intake_flood_paused |
| MON-15 | Ageing incidents unnoticed | ✅ | 72h SLA alarm | test_sweeper_rechecks_stale_and_flags_sla |
| MON-16 | Read-replica lag causes a resubmit | ⚪ | Single SQLite file, no replicas |  |
| MON-17 | Restore from backup or job replay | 🟡 | desk_id in request_ids prevents cross-database collisions; restoring an old backup is not handled specially |  |
| MON-18 | Sandbox/production behaviour gap | 🟡 | Sandbox-verified facts documented in README (failure.message, details.type) |  |
