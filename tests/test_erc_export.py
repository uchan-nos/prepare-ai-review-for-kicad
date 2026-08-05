import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import erc_export


class ParseNetlistTests(unittest.TestCase):
    def test_parses_components_pins_and_power_nets(self):
        root = ET.fromstring('''\
<export>
  <components>
    <comp ref="U1"><value>MCU</value><libsource part="STM32"/>
      <sheetpath names="/Main/"/></comp>
  </components>
  <nets>
    <net name="+3V3"><node ref="#PWR01" pin="1" pintype="power_out"/>
      <node ref="U1" pin="1" pintype="power_in" pinfunction="VDD"/></net>
  </nets>
</export>''')

        components, pins_by_comp, nets, power_nets = erc_export.parse_netlist(root)

        self.assertEqual(
            components['U1'],
            {'value': 'MCU', 'libpart': 'STM32', 'sheet': '/Main/'},
        )
        self.assertEqual(pins_by_comp['U1']['1']['net'], '+3V3')
        self.assertEqual(nets['+3V3'][1]['pinfunction'], 'VDD')
        self.assertEqual(power_nets, {'+3V3'})


class ReviewFormattingTests(unittest.TestCase):
    def test_numeric_pins_sort_before_non_numeric_pins(self):
        review = erc_export.format_review(
            'board.kicad_sch',
            {'U1': {'value': 'IC', 'libpart': 'IC', 'sheet': '/'}},
            {'U1': {
                'A': {'net': 'A_NET', 'pintype': 'input', 'pinfunction': 'A'},
                '10': {'net': 'TEN_NET', 'pintype': 'input', 'pinfunction': 'TEN'},
                '2': {'net': 'TWO_NET', 'pintype': 'input', 'pinfunction': 'TWO'},
            }},
            {}, set(), [],
        )

        self.assertLess(review.index('pin2('), review.index('pin10('))
        self.assertLess(review.index('pin10('), review.index('pinA('))


class MainTests(unittest.TestCase):
    def test_cancelled_erc_still_writes_full_review(self):
        netlist = '''\
<export>
  <design><source>{source}</source></design>
  <components>
    <comp ref="R1"><value>10k</value><libsource part="R"/>
      <sheetpath names="/"/></comp>
  </components>
  <nets>
    <net name="SIG"><node ref="R1" pin="1" pintype="passive"/></net>
  </nets>
</export>'''

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / 'board.kicad_sch'
            input_net = tmp_path / 'input.net'
            input_net.write_text(netlist.format(source=source), encoding='utf-8')

            with (
                mock.patch.object(erc_export, '_FALLBACK_LOG', str(tmp_path / 'error.log')),
                mock.patch.object(erc_export, 'find_kicad_cli', return_value='kicad-cli'),
                mock.patch.object(
                    erc_export, '_run_cmd_with_progress',
                    return_value=subprocess.CompletedProcess([], -1, None, 'cancelled'),
                ),
                mock.patch.object(sys, 'argv', ['erc_export.py', '--review-only', str(input_net)]),
            ):
                erc_export.main()

            reviews = list((tmp_path / 'ai-review').glob('*.review.txt'))
            self.assertEqual(len(reviews), 1)
            review = reviews[0].read_text(encoding='utf-8')
            self.assertIn('KiCad ERC はユーザーによりキャンセルされました。', review)
            self.assertIn('== COMPONENTS ==', review)
            self.assertIn('R1  10k  (R)', review)
            self.assertIn('== SIGNAL NETS ==', review)


if __name__ == '__main__':
    unittest.main()
