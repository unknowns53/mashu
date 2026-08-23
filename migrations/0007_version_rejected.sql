-- A rejected proposal leaves its candidate version behind, and that version
-- needs a state of its own. It was moved to dormant, which said the wrong
-- thing: dormant is a judgement about the content (not useful now, worth
-- re-evaluating later), while rejection is a judgement about the proposal.
-- Conflating them would make any future sweep for re-evaluation candidates
-- pull in the leftovers of every turned-down proposal.
--
-- rejected is also the one version status no agent may propose. completed,
-- disproven and dormant are readings of the knowledge that an agent can put
-- forward; rejected is the reviewer's act, so only proposals.reject writes it.

ALTER TABLE memory_version DROP CONSTRAINT memory_version_status_check;
ALTER TABLE memory_version ADD CONSTRAINT memory_version_status_check
    CHECK (status IN ('candidate', 'superseded', 'disproven',
                      'dormant', 'rejected', 'completed'));
