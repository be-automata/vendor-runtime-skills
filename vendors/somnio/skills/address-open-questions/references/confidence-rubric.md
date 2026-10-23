# Confidence rubric

Every option on a question card carries a score from 0 to 10: how confident the
skill is that **this option is the right one for this situation**. Scores are
independent judgments, not probabilities, so they do not sum to 10. The
recommendation is the highest score.

## Bands

| Score | Meaning | Typical evidence |
|:------|:--------|:-----------------|
| 9-10 | Would be surprised to be wrong | Repository evidence and a primary source (vendor docs, RFC, the ADR itself) agree; no open fact; precedent matches closely |
| 7-8 | Strong, one minor unknown | Good evidence on both sides, or one side plus a close precedent; the unknown would not flip the choice |
| 5-6 | Reasonable, but a real unknown | Mixed sources, partial repository evidence, or precedent from a different scale or stack |
| 3-4 | Mostly judgment | Little evidence; the option is defensible but another person could fairly pick differently |
| 0-2 | A guess | Do not recommend it; say what would have to be learned first |

## Ceilings

Apply every ceiling that holds; the lowest wins.

| Condition | Ceiling |
|:----------|:--------|
| No primary source (only blogs, forums, model knowledge) | 8 |
| No repository evidence (no repo, or the code path was not read) | 6 |
| An open **Blocked on a fact** item that could flip the choice | 5 |
| Depends on a rule marked **[UNCONFIRMED]** | current score − 2 |
| Research agent fell back from `deep-research`, or stalled and was replaced by an inline search, and found under three sources | 6 |

## Writing the "why not higher" line

One line, naming the specific gap: "6 — the load balancer's idle timeout is not
in the repo; at 350 s this option is wrong." Never "some uncertainty remains".

## Calibration check

Before printing a card, look at its scores together:

- All options within one point of each other → the evidence does not separate
  them. Say so in the recommendation and lean on reversibility and cost.
- Every option ≥ 7 → either the question does not matter much (say so on the
  card) or the scores are inflated. Re-read the ceilings.
- The stated default scores ≤ 4 → flag it at the top of the card: the default
  will apply if nobody acts, and the skill believes it is wrong.
