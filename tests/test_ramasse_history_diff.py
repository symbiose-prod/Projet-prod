"""Tests for common/ramasse_history.diff_ramasse_lines — pure logic, no DB."""
from __future__ import annotations

from common.ramasse_history import diff_ramasse_lines


def _line(ref: str, cartons: int, **extra):
    base = {"ref": ref, "produit": f"Produit {ref}", "ddm": "01/01/2027",
            "cartons": cartons, "palettes": 1, "poids": 100}
    base.update(extra)
    return base


class TestDiffRamasseLines:

    def test_no_old_lines_all_added(self):
        new = [_line("A", 10), _line("B", 20)]
        result = diff_ramasse_lines(None, new)
        assert len(result["added"]) == 2
        assert result["removed"] == []
        assert result["modified"] == []
        assert result["unchanged"] == []

    def test_empty_old_lines_all_added(self):
        new = [_line("A", 10)]
        result = diff_ramasse_lines([], new)
        assert len(result["added"]) == 1
        assert result["added"][0]["ref"] == "A"

    def test_all_unchanged(self):
        lines = [_line("A", 10), _line("B", 20)]
        result = diff_ramasse_lines(lines, lines)
        assert len(result["unchanged"]) == 2
        assert result["added"] == []
        assert result["modified"] == []
        assert result["removed"] == []

    def test_added_line(self):
        old = [_line("A", 10)]
        new = [_line("A", 10), _line("B", 20)]
        result = diff_ramasse_lines(old, new)
        assert len(result["unchanged"]) == 1
        assert result["unchanged"][0]["ref"] == "A"
        assert len(result["added"]) == 1
        assert result["added"][0]["ref"] == "B"

    def test_modified_line_includes_old_cartons(self):
        old = [_line("A", 10)]
        new = [_line("A", 15)]
        result = diff_ramasse_lines(old, new)
        assert len(result["modified"]) == 1
        mod = result["modified"][0]
        assert mod["ref"] == "A"
        assert mod["cartons"] == 15
        assert mod["_old_cartons"] == 10

    def test_removed_line(self):
        old = [_line("A", 10), _line("B", 20)]
        new = [_line("A", 10)]
        result = diff_ramasse_lines(old, new)
        assert len(result["unchanged"]) == 1
        assert len(result["removed"]) == 1
        assert result["removed"][0]["ref"] == "B"

    def test_mixed_scenario(self):
        """Scénario réel : J1 soir = 3 produits, J2 matin = 1 retiré, 1 modifié, 1 ajouté."""
        old = [
            _line("KEF-ORG-12x33", 50),  # va être modifié (50→80)
            _line("KEF-GIN-12x33", 30),  # va être retiré
            _line("KEF-FRA-12x33", 20),  # inchangé
        ]
        new = [
            _line("KEF-ORG-12x33", 80),  # modifié
            _line("KEF-FRA-12x33", 20),  # inchangé
            _line("KEF-POM-6x33", 15),   # nouveau
        ]
        result = diff_ramasse_lines(old, new)

        assert len(result["unchanged"]) == 1
        assert result["unchanged"][0]["ref"] == "KEF-FRA-12x33"

        assert len(result["modified"]) == 1
        assert result["modified"][0]["ref"] == "KEF-ORG-12x33"
        assert result["modified"][0]["_old_cartons"] == 50
        assert result["modified"][0]["cartons"] == 80

        assert len(result["added"]) == 1
        assert result["added"][0]["ref"] == "KEF-POM-6x33"

        assert len(result["removed"]) == 1
        assert result["removed"][0]["ref"] == "KEF-GIN-12x33"

    def test_cartons_none_treated_as_zero(self):
        old = [_line("A", 0)]
        new = [{"ref": "A", "cartons": None, "produit": "x", "ddm": "", "palettes": 0, "poids": 0}]
        result = diff_ramasse_lines(old, new)
        # 0 == 0 → unchanged
        assert len(result["unchanged"]) == 1

    def test_ref_is_string_compared(self):
        """Les refs sont toujours comparées en str (évite les pièges int vs str)."""
        old = [{"ref": 123, "cartons": 10}]
        new = [{"ref": "123", "cartons": 10}]
        result = diff_ramasse_lines(old, new)
        assert len(result["unchanged"]) == 1
