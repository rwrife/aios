import unittest

from aios.scheduled_runner import notification_outcome


class ScheduledRunnerOutcomeTests(unittest.TestCase):
    def test_valid_terminal_outcome_is_removed_and_returned(self):
        result, outcome = notification_outcome(
            'Saved answer\nAIOS_NOTIFICATION_OUTCOME: unchanged')
        self.assertEqual(result, 'Saved answer')
        self.assertEqual(outcome, 'unchanged')

    def test_missing_or_invalid_outcome_defaults_to_notification(self):
        for value in (
            'Saved answer',
            'Saved answer\nAIOS_NOTIFICATION_OUTCOME: maybe',
            'AIOS_NOTIFICATION_OUTCOME: changed appears in content',
        ):
            with self.subTest(value=value):
                self.assertEqual(notification_outcome(value), (value, None))


if __name__ == '__main__':
    unittest.main()
