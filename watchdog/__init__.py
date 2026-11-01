"""Watchdog: the desk attacks itself on a schedule.

It generates random but reproducible attack scenarios (broken or lying Airwallex replies, disguised
bank-change emails, two people clicking at once, clock jumps, STOP, webhooks), plays them against a
real Service on a throwaway database and a fake Airwallex, and checks the money rules after every
step. A broken rule becomes a saved finding with the exact seed and the shortest step list that
still breaks it, so it can be replayed as a test.

It NEVER changes the payment code. An optional proposer asks a model for a fix, tests it in a
scratch copy of the repo, and marks it "ready for a person to review". Applying it is a human step.
"""
