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
            {'value': 'MCU', 'libpart': 'STM32', 'sheet': '/Main/',
             'manufacturer': '', 'manufacturer_pn': '', 'footprint': ''},
        )
        self.assertEqual(pins_by_comp['U1']['1']['net'], '+3V3')
        self.assertEqual(nets['+3V3'][1]['pinfunction'], 'VDD')
        self.assertEqual(power_nets, {'+3V3'})

    def test_parses_manufacturer_fields(self):
        root = ET.fromstring('''\
<export>
  <components>
    <comp ref="U1"><value>MCU</value>
      <fields>
        <field name="Manufacturer">STMicroelectronics</field>
        <field name="Manufacturer PN">STM32F103C8T6</field>
        <field name="Datasheet">~</field>
      </fields>
      <libsource part="STM32"/><sheetpath names="/"/></comp>
  </components>
  <nets/>
</export>''')

        components, _, _, _ = erc_export.parse_netlist(root)

        self.assertEqual(components['U1']['manufacturer'], 'STMicroelectronics')
        self.assertEqual(components['U1']['manufacturer_pn'], 'STM32F103C8T6')


class ManufacturerFormattingTests(unittest.TestCase):
    def _review(self, comp):
        return erc_export.format_review(
            'board.kicad_sch', {'U1': comp},
            {'U1': {'1': {'net': 'SIG', 'pintype': 'input', 'pinfunction': 'IN'}}},
            {}, set(), [],
        )

    def test_manufacturer_fields_are_listed_on_their_own_labelled_lines(self):
        # 略称ではなくフィールド名をそのまま出す。1 行だけ読んで意味が取れることを優先。
        review = self._review({
            'value': 'MCU', 'libpart': 'STM32', 'sheet': '/',
            'manufacturer': 'STMicroelectronics', 'manufacturer_pn': 'STM32F103C8T6',
        })

        self.assertIn('  U1  MCU  (STM32)\n'
                      '    Manufacturer: STMicroelectronics\n'
                      '    Manufacturer PN: STM32F103C8T6\n', review)

    def test_only_available_field_is_listed(self):
        review = self._review({
            'value': 'MCU', 'libpart': 'STM32', 'sheet': '/',
            'manufacturer': '', 'manufacturer_pn': 'STM32F103C8T6',
        })

        self.assertIn('  U1  MCU  (STM32)\n'
                      '    Manufacturer PN: STM32F103C8T6\n', review)
        self.assertNotIn('    Manufacturer:', review)

    def test_no_manufacturer_lines_when_fields_are_absent(self):
        review = self._review({'value': 'MCU', 'libpart': 'STM32', 'sheet': '/'})

        self.assertIn('  U1  MCU  (STM32)\n    pin1(', review)
        self.assertNotIn('    Manufacturer', review)


class FootprintTests(unittest.TestCase):
    """Footprint は部品の物理的な実装形態を示すため、回路レビューにも有用。"""

    FP = 'Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical'

    def test_parses_footprint_element(self):
        root = ET.fromstring(f'''\
<export>
  <components>
    <comp ref="J3"><value>Conn_01x01</value>
      <footprint>{self.FP}</footprint>
      <libsource part="Conn_01x01"/><sheetpath names="/"/></comp>
  </components>
  <nets/>
</export>''')

        components, _, _, _ = erc_export.parse_netlist(root)

        self.assertEqual(components['J3']['footprint'], self.FP)

    def test_missing_footprint_element_yields_empty_string(self):
        root = ET.fromstring('''\
<export>
  <components>
    <comp ref="J3"><value>Conn_01x01</value>
      <libsource part="Conn_01x01"/><sheetpath names="/"/></comp>
  </components>
  <nets/>
</export>''')

        components, _, _, _ = erc_export.parse_netlist(root)

        self.assertEqual(components['J3']['footprint'], '')

    def _review(self, **overrides):
        comp = {'value': '22uH', 'libpart': 'L_Ferrite', 'sheet': '/',
                'manufacturer': '', 'manufacturer_pn': '', 'footprint': ''}
        comp.update(overrides)
        return erc_export.format_review(
            'board.kicad_sch', {'L1': comp},
            {'L1': {'1': {'net': '/DCDC_IN', 'pintype': 'passive', 'pinfunction': '1_1'}}},
            {}, set(), [])

    def test_footprint_line_follows_the_component_header(self):
        review = self._review(footprint='uchan:L_3.0x3.0mm_HandSolder',
                              manufacturer_pn='SRN3012TA-220M')

        self.assertIn('  L1  22uH  (L_Ferrite)\n'
                      '    Footprint: uchan:L_3.0x3.0mm_HandSolder\n'
                      '    Manufacturer PN: SRN3012TA-220M\n', review)

    def test_footprint_precedes_pin_lines(self):
        review = self._review(footprint='uchan:L_3.0x3.0mm_HandSolder')

        self.assertLess(review.index('Footprint:'), review.index('pin1('))

    def test_no_footprint_line_when_unset(self):
        self.assertNotIn('Footprint:', self._review(footprint=''))

    def test_footprint_string_is_emitted_verbatim(self):
        # ツール側で解釈・短縮・正規化しない
        odd = 'My Lib:Odd_Name-1.27mm (variant)'

        self.assertIn(f'    Footprint: {odd}\n', self._review(footprint=odd))


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


class ErcViolationCountTests(unittest.TestCase):
    """kicad-cli の ERC JSON は違反を sheets[].violations に入れる。"""

    def _review(self, erc_data):
        return erc_export.format_review(
            'board.kicad_sch', {}, {}, {}, set(), [], erc_data=erc_data)

    def _header(self, erc_data):
        for line in self._review(erc_data).splitlines():
            if line.startswith('== ERC RESULTS =='):
                return line
        self.fail('ERC RESULTS セクションが無い')

    def test_counts_violations_nested_under_sheets(self):
        erc_data = {'sheets': [
            {'path': '/', 'violations': [
                {'severity': 'warning', 'description': 'w1'},
                {'severity': 'error', 'description': 'e1'},
            ]},
            {'path': '/sub/', 'violations': [
                {'severity': 'warning', 'description': 'w2'},
            ]},
        ]}

        self.assertEqual(self._header(erc_data),
                         '== ERC RESULTS == (1 error(s), 2 warning(s))')

    def test_counts_top_level_violations_too(self):
        erc_data = {'violations': [{'severity': 'error', 'description': 'e1'}]}

        self.assertEqual(self._header(erc_data),
                         '== ERC RESULTS == (1 error(s), 0 warning(s))')

    def test_excluded_violations_are_reported_separately(self):
        erc_data = {'sheets': [{'path': '/', 'violations': [
            {'severity': 'warning', 'description': 'w1'},
            {'severity': 'warning', 'description': 'w2', 'excluded': True},
        ]}]}

        self.assertEqual(self._header(erc_data),
                         '== ERC RESULTS == (0 error(s), 1 warning(s), 1 excluded)')

    def test_no_violations_reports_zero(self):
        self.assertEqual(self._header({'sheets': [{'path': '/', 'violations': []}]}),
                         '== ERC RESULTS == (0 error(s), 0 warning(s))')


class ErcIgnoredCheckTests(unittest.TestCase):
    """無効化された ERC チェックはサマリから見えないと、0 件を過信させる。"""

    IGNORED = [
        {'key': 'single_global_label',
         'description': 'Global label only appears once in the schematic'},
        {'key': 'four_way_junction',
         'description': 'Four connection points are joined together'},
    ]

    def _review(self, erc_data):
        return erc_export.format_review(
            'board.kicad_sch', {}, {}, {}, set(), [], erc_data=erc_data)

    def _header(self, erc_data):
        for line in self._review(erc_data).splitlines():
            if line.startswith('== ERC RESULTS =='):
                return line
        self.fail('ERC RESULTS セクションが無い')

    def test_header_reports_ignored_check_count(self):
        header = self._header({'sheets': [], 'ignored_checks': self.IGNORED})

        self.assertEqual(header,
                         '== ERC RESULTS == (0 error(s), 0 warning(s), 2 checks ignored)')

    def test_ignored_checks_are_listed_with_key_and_description(self):
        review = self._review({'sheets': [], 'ignored_checks': self.IGNORED})

        self.assertIn(
            '  - single_global_label: Global label only appears once in the schematic',
            review)
        self.assertIn(
            '  - four_way_junction: Four connection points are joined together', review)

    def test_ignored_list_precedes_the_json_block(self):
        review = self._review({'sheets': [], 'ignored_checks': self.IGNORED})

        self.assertLess(review.index('single_global_label'), review.index('```json'))

    def test_nothing_added_when_no_checks_are_ignored(self):
        for erc_data in ({'sheets': []}, {'sheets': [], 'ignored_checks': []}):
            with self.subTest(erc_data=erc_data):
                self.assertEqual(self._header(erc_data),
                                 '== ERC RESULTS == (0 error(s), 0 warning(s))')
                self.assertNotIn('無効化された ERC チェック (', self._review(erc_data))


class TwoPinPowerAnomalyTests(unittest.TestCase):
    """電源ネット間の 2 端子部品。デカップリング等の正常な構成を誤検出しないこと。"""

    POWER_NETS = {'+3V3', '+5V', 'GND', 'AGND'}

    def _anomalies(self, ref, value, net_a, net_b):
        components = {ref: {'value': value, 'libpart': ref[:1], 'sheet': '/'}}
        pins_by_comp = {ref: {
            '1': {'net': net_a, 'pintype': 'passive', 'pinfunction': ''},
            '2': {'net': net_b, 'pintype': 'passive', 'pinfunction': ''},
        }}
        return erc_export.detect_anomalies(components, pins_by_comp, set(self.POWER_NETS))

    # --- 正常な構成: 検出しない ---

    def test_decoupling_capacitor_is_not_flagged(self):
        self.assertEqual(self._anomalies('C1', '100nF', '+3V3', 'GND'), [])

    def test_pulldown_resistor_is_not_flagged(self):
        self.assertEqual(self._anomalies('R1', '10k', '+3V3', 'GND'), [])

    def test_protection_diode_to_gnd_is_not_flagged(self):
        self.assertEqual(self._anomalies('D1', 'TVS', '+5V', 'GND'), [])

    def test_ferrite_bead_between_two_supplies_is_not_flagged(self):
        self.assertEqual(self._anomalies('L1', 'BLM18', '+3V3', '+5V'), [])

    def test_link_between_two_grounds_is_not_flagged(self):
        self.assertEqual(self._anomalies('L2', 'BLM18', 'GND', 'AGND'), [])

    def test_series_fuse_between_two_supplies_is_not_flagged(self):
        self.assertEqual(self._anomalies('F1', '1A', '+5V', '+3V3'), [])

    # --- 疑わしい構成: 検出する ---

    def test_inductor_shorting_supply_to_gnd_is_flagged(self):
        anomalies = self._anomalies('L1', 'BLM18', '+3V3', 'GND')

        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0][0], 'WARNING')
        self.assertIn('L1', anomalies[0][1])

    def test_fuse_shorting_supply_to_gnd_is_flagged(self):
        self.assertEqual(len(self._anomalies('F1', '1A', '+5V', 'GND')), 1)

    def test_zero_ohm_resistor_shorting_supply_to_gnd_is_flagged(self):
        for value in ('0', '0R', '0R0', '0Ω'):
            with self.subTest(value=value):
                self.assertEqual(len(self._anomalies('R9', value, '+3V3', 'GND')), 1)

    def test_capacitor_bridging_two_supplies_is_flagged(self):
        self.assertEqual(len(self._anomalies('C9', '100nF', '+3V3', '+5V')), 1)

    def test_component_with_both_pins_on_same_net_is_flagged(self):
        anomalies = self._anomalies('C1', '100nF', 'GND', 'GND')

        self.assertEqual(len(anomalies), 1)
        self.assertIn('C1', anomalies[0][1])


class SameNetShortTests(unittest.TestCase):
    """2 端子部品の両端が同じネットなら、その部品は短絡されていて機能しない。"""

    def _anomalies(self, ref, value, net_a, net_b):
        components = {ref: {'value': value, 'libpart': ref[:1], 'sheet': '/'}}
        pins_by_comp = {ref: {
            '1': {'net': net_a, 'pintype': 'passive', 'pinfunction': ''},
            '2': {'net': net_b, 'pintype': 'passive', 'pinfunction': ''},
        }}
        return erc_export.detect_anomalies(components, pins_by_comp, {'+3V3', 'GND'})

    def test_resistor_shorted_by_signal_net_is_flagged(self):
        anomalies = self._anomalies('R1', '10k', 'Net-(U1-FB)', 'Net-(U1-FB)')

        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0][0], 'WARNING')
        self.assertIn('R1', anomalies[0][1])
        self.assertIn('Net-(U1-FB)', anomalies[0][1])

    def test_capacitor_shorted_by_signal_net_is_flagged(self):
        self.assertEqual(len(self._anomalies('C1', '100nF', 'SIG', 'SIG')), 1)

    def test_shorted_component_on_power_net_is_still_flagged(self):
        self.assertEqual(len(self._anomalies('L1', '22uH', 'GND', 'GND')), 1)

    def test_distinct_signal_nets_are_not_flagged(self):
        self.assertEqual(self._anomalies('R1', '10k', 'SIG_A', 'SIG_B'), [])

    def test_unconnected_pins_are_not_flagged(self):
        # 未接続ピンには KiCad がピンごとに別のネット名を振るので同一にならない
        self.assertEqual(
            self._anomalies('R1', '10k', 'unconnected-(R1-Pad1)', 'unconnected-(R1-Pad2)'),
            [])

    def test_non_passive_two_pin_component_is_not_flagged(self):
        # 2 ピンコネクタの両ピンが GND、のような構成は意図的でありうる
        self.assertEqual(self._anomalies('J1', 'Conn_01x02', 'GND', 'GND'), [])


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
