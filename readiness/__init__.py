"""Provisioning readiness pass (Addendum 64/65, WAL-112).

Harness code only — not product code. It drives the measured
run's own sandboxed provisioning path to provision every
registered unit's sides and records readiness rows. It writes
no measured records and no scores; its output is labelled
"readiness" (see readiness/provision.py).
"""
