"""src/funding.py — funding facts from a funding card's headline (cases are real prod headlines)."""
import unittest

from src import funding


def p(headline, *names):
    return funding.parse(headline, list(names), {})


class FundingTest(unittest.TestCase):
    def test_round_with_amount_and_series(self):
        f = p("Neura Robotics Secures $1.4 Billion Series C Funding from Tether, Amazon, Nvidia, Qualcomm",
              "Neura Robotics", "NVIDIA")
        self.assertEqual((f["kind"], f["target"], f["amount_usd"], f["round"]),
                         ("round", "Neura Robotics", 1_400_000_000, "Series C"))

    def test_currency_converted_and_kept(self):
        f = p("Flexoo Secures €11M Series A to Advance Flexible Sensors for Physical AI", "Flexoo")
        self.assertEqual((f["amount"], f["currency"], f["amount_usd"]), (11_000_000, "EUR", 11_880_000))
        f = p("MoSense Secures RMB 100M Angel+ Funding for Humanoid Robot Tactile Sensors", "MoSense")
        self.assertEqual((f["currency"], f["amount_usd"]), ("CNY", 14_000_000))

    def test_yen_vs_yuan(self):
        self.assertEqual(p("Mujin Secures ¥10 Billion from Japanese Investors", "Mujin")["currency"], "JPY")
        self.assertEqual(p("Kawasaki Robotics Unit Raises 5 Billion Yen", "Kawasaki")["amount_usd"], 33_500_000)
        self.assertEqual(p("Galbot Secures ¥1 Billion Series B", "Galbot")["currency"], "CNY")

    def test_number_words_and_bare_suffix(self):
        self.assertEqual(p("Agile Robots Secures Five Hundred Million Series F Led By SoftBank And NVIDIA",
                           "Agile Robots", "SoftBank")["amount_usd"], 500_000_000)
        self.assertEqual(p("Machenta Raises Forty Million Dollars to Develop Instant Learning Robotics Models",
                           "Machenta")["amount_usd"], 40_000_000)
        self.assertEqual(p("Nidus Secures 30M Series A for Defense Wire Harness Automation", "Nidus")["amount_usd"],
                         30_000_000)

    def test_raise_not_valuation(self):
        f = p("Apptronik Secures $935M Series A, Reaching $5B Valuation with Google and Mercedes-Benz Investment",
              "Apptronik", "Google")
        self.assertEqual((f["target"], f["amount_usd"]), ("Apptronik", 935_000_000))

    def test_valuation_only(self):
        f = p("XPeng Robotics Achieves $6.3 Billion Valuation, Intensifying Humanoid Robot Race", "XPeng")
        self.assertEqual((f["kind"], f["round"]), ("valuation", None))

    def test_investor_headline_targets_the_investee(self):
        self.assertEqual(p("Nvidia Invests $3.5 Billion in MediaTek to Co-Develop Next-Gen AI Platforms",
                           "NVIDIA", "MediaTek")["target"], "MediaTek")
        self.assertEqual(p("Samsung Co-Leads $231M Series A for Euclide's Robotic AI Chips",
                           "Samsung Electronics", "Euclide")["target"], "Euclide")
        # the investee is not a known company → unknown, never the investor
        self.assertIsNone(p("ARK Innovation Invests $21.5M in Crypto Infrastructure, Validating AI-Blockchain Convergence",
                            "ARK Innovation")["target"])

    def test_possessive_owner_is_not_the_target(self):
        self.assertEqual(p("Rivian CEO's Mind Robotics Secures $500M to Build AI Factory Robots",
                           "Rivian", "Mind Robotics")["target"], "Mind Robotics")

    def test_fund_launch(self):
        f = p("K2 Global Launches $200M Physical AI Fund, Backing Agility Robotics and xAI", "K2 Global", "Agility Robotics")
        self.assertEqual((f["kind"], f["target"]), ("fund", "K2 Global"))

    def test_not_a_company_round(self):
        for h in ("Physical AI Startups Secure $47.4 Billion in H1 2026 Funding, Driving Robotics Boom",
                  "Humanoid Robotics Sector Secures Record $8.7 Billion in Venture Capital Funding",
                  "Hanwha Strategic Investment in Raion Robotics Collapses Over Defense Rights",
                  "Unitree Humanoid Robot Suffers Catastrophic Failure, Raising Industry Reliability Concerns",
                  "Gecko Robotics Secures $71 Million U.S. Navy Contract for AI Inspection Robots"):
            self.assertIsNone(p(h, "Unitree", "Hanwha", "Gecko Robotics"), h)

    def test_round_without_amount(self):
        f = p("Humyn Labs Secures Series A Funding For Physical AI Data", "Humyn Labs")
        self.assertEqual((f["kind"], f["amount_usd"], f["round"]), ("round", None, "Series A"))


if __name__ == "__main__":
    unittest.main()
