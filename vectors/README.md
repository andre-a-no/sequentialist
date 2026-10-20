# Test vectors

Files that independent implementations are checked against. They are
generated from the Python code (`tools/make_vectors.py`); CI fails if a
file differs from what the code produces.

- `assignment.json`: the assignment of units to variants
  (`sequentialist.ab.assign`, `@sequentialist/ab-client`). Every case must
  be reproduced exactly, including the probability as a binary64 number.

This directory is licensed under the Apache License 2.0 ([LICENSE](LICENSE)),
so that implementations under any license can use the vectors in their tests.
