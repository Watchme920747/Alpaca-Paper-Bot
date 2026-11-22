import unittest
from bot import update_simulations, performance

class SimulationTests(unittest.TestCase):
    def test_no_same_bar_outcome(self):
        state = {"simulations": [{"symbol":"BTCUSD","entry_bar":"2026-11-22T10:00:00Z",
            "entry_price":100,"stop":95,"target":110,"status":"open","bars_held":0}]}
        update_simulations(state, {"BTCUSD":{"bar":"2026-11-22T10:00:00Z","price":120}})
        self.assertEqual(state["simulations"][0]["status"], "open")
        update_simulations(state, {"BTCUSD":{"bar":"2026-11-22T11:00:00Z","price":111}})
        self.assertEqual(state["simulations"][0]["status"], "closed")

    def test_position_size_requires_history(self):
        self.assertEqual(performance({})["paper_allocation_cap"], 10)
        wins = [{"status":"closed","return_pct":1.0} for _ in range(20)]
        self.assertEqual(performance({"simulations":wins})["paper_allocation_cap"], 25)
        losses = [{"status":"closed","return_pct":-1.0} for _ in range(10)]
        self.assertEqual(performance({"simulations":losses})["paper_allocation_cap"], 5)

if __name__ == "__main__":
    unittest.main()
