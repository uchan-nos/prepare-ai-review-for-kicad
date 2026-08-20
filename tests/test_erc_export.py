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
            {'value': 'MCU', 'libpart': 'STM32', 'lib': '', 'description': '',
             'sheet': '/Main/', 'purpose': '', 'manufacturer': '',
             'manufacturer_pn': '', 'footprint': ''},
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


class PurposeFieldTests(unittest.TestCase):
    """Purpose は「この回路で何のために使うか」という作成者の設計意図。

    Value（何という部品か）や Footprint（何を実装するか）からは読み取れない
    情報なので、設定されていればレビューに渡す。
    """

    PURPOSE = 'オシロスコープを当てるデバッグ端子'

    def test_parses_purpose_field(self):
        root = ET.fromstring('''\
<export>
  <components>
    <comp ref="J2"><value>Conn_01x10</value>
      <fields>
        <field name="Purpose">オシロスコープを当てるデバッグ端子</field>
      </fields>
      <libsource lib="Connector_Generic" part="Conn_01x10"/>
      <sheetpath names="/"/></comp>
    <comp ref="R1"><value>10k</value>
      <libsource lib="Device" part="R"/><sheetpath names="/"/></comp>
  </components>
  <nets/>
</export>''')

        components, _, _, _ = erc_export.parse_netlist(root)

        self.assertEqual(components['J2']['purpose'], self.PURPOSE)
        self.assertEqual(components['R1']['purpose'], '')

    def _review(self, ref, comp, pins):
        return erc_export.format_review(
            'board.kicad_sch', {ref: comp}, {ref: pins}, {}, set(), [])

    def test_purpose_of_internal_component_sits_between_value_and_footprint(self):
        comp = {'value': 'MCU', 'libpart': 'STM32', 'lib': 'MCU_ST_STM32F1',
                'description': '', 'sheet': '/', 'purpose': 'メイン MCU',
                'footprint': 'Package_QFP:LQFP-48_7x7mm_P0.5mm',
                'manufacturer': '', 'manufacturer_pn': 'STM32F103C8T6'}

        review = self._review(
            'U1', comp,
            {'1': {'net': 'SIG', 'pintype': 'input', 'pinfunction': 'IN'}})

        self.assertIn('  U1  MCU  (STM32)\n'
                      '    Purpose: メイン MCU\n'
                      '    Footprint: Package_QFP:LQFP-48_7x7mm_P0.5mm\n'
                      '    Manufacturer PN: STM32F103C8T6\n', review)

    def test_purpose_of_external_interface_sits_between_value_and_footprint(self):
        comp = {'value': 'Conn_01x10', 'libpart': 'Conn_01x10',
                'lib': 'Connector_Generic', 'description': '', 'sheet': '/',
                'purpose': self.PURPOSE,
                'footprint': 'Connector_PinHeader_2.54mm:'
                             'PinHeader_1x10_P2.54mm_Vertical',
                'manufacturer': '', 'manufacturer_pn': ''}

        review = self._review(
            'J2', comp,
            {'1': {'net': 'VBUS', 'pintype': 'passive', 'pinfunction': 'Pin_1'}})

        self.assertIn('J2  Conn_01x10\n'
                      f'  Purpose: {self.PURPOSE}\n'
                      '  Footprint: Connector_PinHeader_2.54mm:'
                      'PinHeader_1x10_P2.54mm_Vertical\n'
                      '  pin1=VBUS\n', review)

    def test_no_purpose_line_when_the_field_is_unset(self):
        comp = {'value': '10k', 'libpart': 'R', 'lib': 'Device',
                'description': '', 'sheet': '/', 'purpose': '', 'footprint': '',
                'manufacturer': '', 'manufacturer_pn': ''}

        review = self._review(
            'R1', comp,
            {'1': {'net': 'SIG', 'pintype': 'passive', 'pinfunction': ''}})

        self.assertNotIn('Purpose', review[review.index('== ANOMALIES =='):])


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


class ExternalInterfaceTests(unittest.TestCase):
    """コネクタ・テストポイントは回路と外部との境界。独立したセクションに出す。

    判定は Library Symbol（lib / part / description）を主、ref を補助に使う。
    誤判定しても INTERNAL COMPONENTS 側に出るだけで情報は落ちないが、
    素直なコネクタが内部部品に混ざると境界のレビューが漏れる。
    """

    GROVE = {
        'value': 'Conn_Grove_Dev', 'libpart': 'Conn_Grove_Dev', 'lib': 'uchan',
        'description': 'Grove 4-pin connector', 'sheet': '/',
        'footprint': 'uchan:Grove_1x04_P2mm_THT_Horizontal',
        'manufacturer': '', 'manufacturer_pn': '',
    }
    GROVE_PINS = {
        '1': {'net': 'Net-(J1-RX)', 'pintype': 'input', 'pinfunction': 'RX'},
        '2': {'net': 'Net-(J1-TX)', 'pintype': 'output', 'pinfunction': 'TX'},
        '3': {'net': 'VBUS', 'pintype': 'power_in', 'pinfunction': 'VCC'},
        '4': {'net': 'GND', 'pintype': 'power_in', 'pinfunction': 'GND'},
    }
    HEADER = {
        'value': 'Conn_01x10', 'libpart': 'Conn_01x10', 'lib': 'Connector_Generic',
        'description': 'Generic connector, single row, 01x10, script generated',
        'sheet': '/',
        'footprint': 'Connector_PinHeader_2.54mm:PinHeader_1x10_P2.54mm_Vertical',
        'manufacturer': '', 'manufacturer_pn': '',
    }
    HEADER_PINS = {
        '1': {'net': 'VBUS', 'pintype': 'passive', 'pinfunction': 'Pin_1'},
        '2': {'net': '/DCDC_IN', 'pintype': 'passive', 'pinfunction': 'Pin_2'},
        '10': {'net': '/SW', 'pintype': 'passive', 'pinfunction': 'Pin_10'},
    }
    RESISTOR = {
        'value': '10k', 'libpart': 'R', 'lib': 'Device', 'description': 'Resistor',
        'sheet': '/', 'footprint': '', 'manufacturer': '', 'manufacturer_pn': '',
    }
    RESISTOR_PINS = {'1': {'net': 'SIG', 'pintype': 'passive', 'pinfunction': ''}}

    def _review(self, components, pins_by_comp, power_nets=('VBUS', 'GND')):
        return erc_export.format_review(
            'board.kicad_sch', components, pins_by_comp, {}, set(power_nets), [])

    def _section(self, review, name):
        """`== NAME ==` から次のセクション見出しまでを返す。"""
        lines = review.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith(f'== {name} =='))
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith('== ')), len(lines))
        return '\n'.join(lines[start:end])

    # --- 解析 ---

    def test_parses_libsource_lib_and_description(self):
        root = ET.fromstring('''\
<export>
  <components>
    <comp ref="J2"><value>Conn_01x10</value>
      <libsource lib="Connector_Generic" part="Conn_01x10"
                 description="Generic connector, single row, 01x10"/>
      <sheetpath names="/"/></comp>
  </components>
  <nets/>
</export>''')

        components, _, _, _ = erc_export.parse_netlist(root)

        self.assertEqual(components['J2']['lib'], 'Connector_Generic')
        self.assertEqual(components['J2']['description'],
                         'Generic connector, single row, 01x10')

    # --- 出力フォーマット ---

    def test_connector_lists_footprint_then_pins_in_pin_order(self):
        review = self._review({'J1': self.GROVE}, {'J1': self.GROVE_PINS})

        self.assertIn('J1  Conn_Grove_Dev\n'
                      '  Footprint: uchan:Grove_1x04_P2mm_THT_Horizontal\n'
                      '  pin1(RX,in)=Net-(J1-RX)\n'
                      '  pin2(TX,out)=Net-(J1-TX)\n'
                      '  pin3(VCC,pwr)=VBUS\n'
                      '  pin4(GND,pwr)=GND\n', review)

    def test_power_pins_are_not_grouped_apart_from_signal_pins(self):
        # コネクタは物理的なピン並びが本質なので、電源ピンだけまとめると読めなくなる
        review = self._review({'J1': self.GROVE}, {'J1': self.GROVE_PINS})

        self.assertNotIn('PWR:', self._section(review, 'EXTERNAL INTERFACES'))

    def test_pin_number_restating_pinfunction_is_omitted(self):
        review = self._review({'J2': self.HEADER}, {'J2': self.HEADER_PINS})

        self.assertIn('J2  Conn_01x10\n'
                      '  Footprint: Connector_PinHeader_2.54mm:'
                      'PinHeader_1x10_P2.54mm_Vertical\n'
                      '  pin1=VBUS\n'
                      '  pin2=/DCDC_IN\n'
                      '  pin10=/SW\n', review)

    def test_libpart_is_shown_when_it_differs_from_the_value(self):
        comp = dict(self.HEADER, value='UART header', libpart='Conn_01x10')

        review = self._review({'J2': comp}, {'J2': self.HEADER_PINS})

        self.assertIn('J2  UART header  (Conn_01x10)\n', review)

    def test_manufacturer_fields_are_listed(self):
        # コネクタは INTERNAL COMPONENTS から外れるので、ここで出さないと情報が落ちる
        comp = dict(self.GROVE, manufacturer='Seeed', manufacturer_pn='110990030')

        review = self._review({'J1': comp}, {'J1': self.GROVE_PINS})

        self.assertIn('  Manufacturer: Seeed\n'
                      '  Manufacturer PN: 110990030\n', review)

    def test_anomalous_power_pin_keeps_its_marker(self):
        pins = dict(self.GROVE_PINS,
                    **{'3': {'net': 'GND', 'pintype': 'power_in',
                             'pinfunction': 'VCC'}})

        review = self._review({'J1': self.GROVE}, {'J1': pins})

        self.assertIn('  pin3(VCC,pwr)=GND !!\n', review)

    def test_reports_none_when_there_is_no_external_interface(self):
        review = self._review({'R1': self.RESISTOR}, {'R1': self.RESISTOR_PINS})

        self.assertEqual(self._section(review, 'EXTERNAL INTERFACES').splitlines()[1],
                         '(none detected)')

    # --- 内部部品との切り分け ---

    def test_connector_is_excluded_from_internal_components(self):
        review = self._review({'J1': self.GROVE, 'R1': self.RESISTOR},
                              {'J1': self.GROVE_PINS, 'R1': self.RESISTOR_PINS})
        internal = self._section(review, 'INTERNAL COMPONENTS')

        self.assertNotIn('J1', internal)
        self.assertIn('R1  10k  (R)', internal)

    def test_internal_component_is_not_listed_as_external(self):
        review = self._review({'R1': self.RESISTOR}, {'R1': self.RESISTOR_PINS})

        self.assertNotIn('R1', self._section(review, 'EXTERNAL INTERFACES'))

    def test_components_section_is_renamed_to_internal_components(self):
        review = self._review({'R1': self.RESISTOR}, {'R1': self.RESISTOR_PINS})

        self.assertIn('== INTERNAL COMPONENTS ==', review)
        self.assertNotIn('== COMPONENTS ==', review)

    def test_sections_run_external_then_internal_then_signal_nets(self):
        # 外部との境界を先に読ませる構成の方がレビューが速い
        review = self._review({'J1': self.GROVE, 'R1': self.RESISTOR},
                              {'J1': self.GROVE_PINS, 'R1': self.RESISTOR_PINS})

        self.assertLess(review.index('== EXTERNAL INTERFACES =='),
                        review.index('== INTERNAL COMPONENTS =='))
        self.assertLess(review.index('== INTERNAL COMPONENTS =='),
                        review.index('== SIGNAL NETS =='))

    # --- 判定 ---

    def test_connector_library_is_external_whatever_the_reference_is(self):
        comp = dict(self.HEADER, lib='Connector', libpart='USB_C_Receptacle',
                    value='USB_C_Receptacle', description='USB Type-C receptacle')

        review = self._review({'USB1': comp}, {'USB1': self.HEADER_PINS})

        self.assertIn('USB1  USB_C_Receptacle\n',
                      self._section(review, 'EXTERNAL INTERFACES'))

    def test_custom_library_connector_is_detected_by_symbol_name(self):
        review = self._review({'J1': self.GROVE}, {'J1': self.GROVE_PINS})

        self.assertIn('J1  Conn_Grove_Dev\n',
                      self._section(review, 'EXTERNAL INTERFACES'))

    def test_test_point_is_external(self):
        comp = dict(self.RESISTOR, lib='uchan', libpart='TestPoint',
                    value='TestPoint', description='test point')

        review = self._review({'TP1': comp}, {'TP1': self.RESISTOR_PINS})

        self.assertIn('TP1  TestPoint\n',
                      self._section(review, 'EXTERNAL INTERFACES'))

    def test_connector_shaped_reference_alone_is_not_enough(self):
        # J9 に割り当てられた抵抗を境界として扱ってはならない
        review = self._review({'J9': self.RESISTOR}, {'J9': self.RESISTOR_PINS})

        self.assertNotIn('J9', self._section(review, 'EXTERNAL INTERFACES'))
        self.assertIn('J9  10k  (R)', self._section(review, 'INTERNAL COMPONENTS'))

    def test_solder_jumper_is_not_external(self):
        # JP1 のようなジャンパは外部との境界ではない
        comp = dict(self.RESISTOR, lib='Jumper', libpart='SolderJumper_2_Open',
                    value='SolderJumper_2_Open', description='solder jumper, 2 pads')

        review = self._review({'JP1': comp}, {'JP1': self.RESISTOR_PINS})

        self.assertNotIn('JP1', self._section(review, 'EXTERNAL INTERFACES'))


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
            self.assertIn('== INTERNAL COMPONENTS ==', review)
            self.assertIn('R1  10k  (R)', review)
            self.assertIn('== SIGNAL NETS ==', review)


if __name__ == '__main__':
    unittest.main()
