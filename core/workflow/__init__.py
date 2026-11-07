"""Workflow engine: approval chains for any request type, configured on screen.

- models.py    definitions (versioned), instances, tasks, delegations
- registry.py  request types and their handlers (registered by modules in hooks.py)
- service.py   submit, approve, reject, cancel, reassign, delegate, escalate
"""
