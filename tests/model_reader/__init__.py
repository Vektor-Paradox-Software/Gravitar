"""SPEC-17 static-reader test suite.

- ``differential.py`` — core harness: compares a fixture's static-reader
  schema against a sandboxed subprocess import-reader schema, applies
  the §10.1 pass rule (every column either matches truth OR carries a
  scoped note; a silent mismatch is a P0 defect).
- ``audit_hook.py`` — ``sys.addaudithook`` sentinel used by the
  zero-execution test to catch any attempt to run project code.
- ``fixtures/`` — numbered subprojects F-01…F-24, each ships with
  expected schema and expected notes.
"""
