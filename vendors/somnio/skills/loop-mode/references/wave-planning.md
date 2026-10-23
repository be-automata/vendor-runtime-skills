# Wave planning — dependency vocabulary for parallel layers

(Adapted from the reconciled line's workflow-builder plan reference, Step 2b; the CLI-specific
parts are dropped. Use this vocabulary when reasoning about Step 5's topological layering and
when explaining the layer plan to the user.)

Steps/tickets run in parallel waves. **Minimize dependencies to maximize parallelism**:

- Items with **no `needs`** → wave 1 (run concurrently).
- Items that depend only on wave-1 items → wave 2, and so on.
- A final synthesis/report step → `needs: all`.

`needs` vocabulary (1-based):

| Value | Meaning |
|---|---|
| omitted | independent — wave 1 |
| `needs: [1, 3]` | depends on items 1 and 3 |
| `needs: all` | depends on ALL previous items |
| `needs: previous` | depends on just the preceding item |
| `needs: 1` | depends on item 1 only |

DAG-validity checklist before dispatching a layer:

- [ ] No cycle (a ticket cannot `need` a later wave).
- [ ] Every `needs` target exists in the set.
- [ ] Each dependent ticket's `parentBranch` is the branch of a ticket in an **earlier** wave.
- [ ] Nothing shares a file across the same wave unless the ownership map assigns one owner.
