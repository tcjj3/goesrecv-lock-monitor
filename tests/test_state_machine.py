import unittest

from goesrecv_lock_monitor import LockStateMachine, reception_is_normal


class LockStateMachineTests(unittest.TestCase):
    def make_machine(self):
        return LockStateMachine({
            "loss_confirm_seconds": 5,
            "recovery_confirm_seconds": 5,
            "alert_cooldown_seconds": 60,
            "history_limit": 10,
            "notify_on_initial_state": False,
        })

    def test_flapping_does_not_transition(self):
        m = self.make_machine()
        self.assertIsNone(m.observe(True, 0))
        self.assertIsNone(m.observe(True, 5))  # initial state confirmed, no notification
        self.assertTrue(m.confirmed)

        self.assertIsNone(m.observe(False, 10))
        self.assertIsNone(m.observe(True, 12))
        self.assertIsNone(m.observe(False, 13))
        self.assertIsNone(m.observe(True, 15))
        self.assertTrue(m.confirmed)
        self.assertEqual(len(m.lost_history), 0)

    def test_loss_and_recovery_require_dwell(self):
        m = self.make_machine()
        m.observe(True, 0)
        m.observe(True, 5)
        self.assertTrue(m.confirmed)

        self.assertIsNone(m.observe(False, 10))
        loss = m.observe(False, 15)
        self.assertIsNotNone(loss)
        self.assertFalse(m.confirmed)
        self.assertEqual(len(m.lost_history), 1)

        self.assertIsNone(m.observe(True, 20))
        recovery = m.observe(True, 25)
        self.assertIsNotNone(recovery)
        self.assertTrue(m.confirmed)
        self.assertEqual(len(m.recovered_history), 1)

    def test_original_reception_criterion(self):
        self.assertTrue(reception_is_normal({
            "reed_solomon_errors": 0,
            "ok": 1,
        }))
        self.assertFalse(reception_is_normal({
            "reed_solomon_errors": -1,
            "ok": 0,
        }))
        self.assertFalse(reception_is_normal({
            "reed_solomon_errors": 11,
            "ok": 1,
        }))


if __name__ == "__main__":
    unittest.main()
