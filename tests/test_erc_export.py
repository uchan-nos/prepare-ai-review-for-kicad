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


class PinNumberSuffixTests(unittest.TestCase):
    """KiCad 10 のネットリストは pinfunction を「ピン名_ピン番号」で出す。

    行頭に pinN があるので末尾の _N は情報を持たない。実データでは名前付きの
    全ピン行（PWR: pin2(GND_2)→GND、pin1(SW_1,out)=... など）に現れるため、
    落とさないとレビューファイル全体に冗長な文字が散る。
    """

    def _review(self, pins):
        comp = {'value': 'DCDC', 'libpart': 'AP3012', 'lib': 'Regulator_Switching',
                'description': '', 'sheet': '/', 'purpose': '', 'footprint': '',
                'manufacturer': '', 'manufacturer_pn': ''}
        return erc_export.format_review(
            'board.kicad_sch', {'U1': comp}, {'U1': pins}, {}, {'GND'}, [])

    def test_signal_pin_line_drops_the_redundant_pin_number(self):
        review = self._review(
            {'1': {'net': '/SW', 'pintype': 'output', 'pinfunction': 'SW_1'}})

        self.assertIn('    pin1(SW,out)=/SW\n', review)

    def test_power_pin_line_drops_the_redundant_pin_number(self):
        review = self._review(
            {'2': {'net': 'GND', 'pintype': 'power_in', 'pinfunction': 'GND_2'}})

        self.assertIn('    pin2(GND,pwr)=GND\n', review)

    def test_suffix_matching_another_pin_number_is_kept(self):
        # pin7 の "FB_3" の _3 は 7 番ピンの番号ではなく名前の一部なので残す
        review = self._review(
            {'7': {'net': 'Net-(U1-FB)', 'pintype': 'input', 'pinfunction': 'FB_3'}})

        self.assertIn('    pin7(FB_3,in)=Net-(U1-FB)\n', review)

    def test_name_without_suffix_is_untouched(self):
        review = self._review(
            {'1': {'net': '/SW', 'pintype': 'output', 'pinfunction': 'SW'}})

        self.assertIn('    pin1(SW,out)=/SW\n', review)


class InternalPinAnnotationTests(unittest.TestCase):
    """注記の省略規則は内部部品にも同じく適用する。

    括弧の有無が情報の有無と一致することが狙い。括弧が無いピンは
    「名前も型も語ることが無い」ことを意味し、型が書かれていないピンは
    passive（または unspecified）だと読み手が復元できる。
    """

    def _review(self, pins, value='10k', libpart='R'):
        comp = {'value': value, 'libpart': libpart, 'lib': 'Device',
                'description': '', 'sheet': '/', 'purpose': '', 'footprint': '',
                'manufacturer': '', 'manufacturer_pn': ''}
        return erc_export.format_review(
            'board.kicad_sch', {'X1': comp}, {'X1': pins}, {}, {'GND'}, [])

    def test_nameless_passive_pin_drops_the_whole_annotation(self):
        review = self._review(
            {'1': {'net': 'SIG', 'pintype': 'passive', 'pinfunction': ''}})
        # 凡例にも pin1(...) の例文があるので、部品一覧の中だけを見る
        section = review[review.index('== INTERNAL COMPONENTS =='):]

        self.assertIn('    pin1=SIG\n', section)
        self.assertNotIn('pin1(', section)

    def test_pin_named_after_its_number_drops_the_whole_annotation(self):
        # 実データのフェライトビード: pinfunction="1_1" / "2_2"
        review = self._review(
            {'1': {'net': '/DCDC_IN', 'pintype': 'passive', 'pinfunction': '1_1'},
             '2': {'net': '/SW', 'pintype': 'passive', 'pinfunction': '2_2'}},
            value='22uH', libpart='L')

        self.assertIn('    pin1=/DCDC_IN\n    pin2=/SW\n', review)

    def test_parens_always_carry_both_name_and_type(self):
        # ダイオードの K / A は役割を語るので括弧を出す。片方だけの (K) は作らない
        review = self._review(
            {'1': {'net': '/DCDC_OUT', 'pintype': 'passive', 'pinfunction': 'K_1'},
             '2': {'net': '/SW', 'pintype': 'passive', 'pinfunction': 'A_2'}},
            value='B5819W', libpart='D_Schottky')

        self.assertIn('    pin1(K,pas)=/DCDC_OUT\n    pin2(A,pas)=/SW\n', review)

    def test_nameless_pin_with_a_meaningful_type_leaves_the_name_slot_empty(self):
        # 括弧の項目数は変えない。名前の欄が空であること自体が「名前が無い」の意
        review = self._review(
            {'1': {'net': 'SIG', 'pintype': 'input', 'pinfunction': 'Pin_1_1'}})

        self.assertIn('    pin1(,in)=SIG\n', review)

    def test_informative_pintype_is_still_shown(self):
        review = self._review(
            {'3': {'net': 'Net-(U1-FB)', 'pintype': 'input', 'pinfunction': 'FB_3'}},
            value='AP3012', libpart='AP3012')

        self.assertIn('    pin3(FB,in)=Net-(U1-FB)\n', review)

    def test_power_pins_are_plain_pin_lines(self):
        # 電源ピンだけの別書式（PWR: 行）は持たない。pwr と書いてあれば足りる
        review = self._review(
            {'1': {'net': 'GND', 'pintype': 'power_in', 'pinfunction': ''},
             '2': {'net': 'GND', 'pintype': 'power_in', 'pinfunction': 'GND_2'}})

        self.assertIn('    pin1(,pwr)=GND\n    pin2(GND,pwr)=GND\n', review)
        self.assertNotIn('PWR:', review)

    def test_nothing_is_marked_as_verified(self):
        """✓ は「検査して正常」を主張するが、チェックはその裏付けほど深くない。

        印が無い = 何も検出されていない、という読み方に統一する。
        """
        review = self._review(
            {'1': {'net': 'GND', 'pintype': 'power_in', 'pinfunction': 'GND_1'}})

        self.assertNotIn('✓', review)

    def test_reversed_power_pin_still_gets_the_anomaly_marker(self):
        review = self._review(
            {'1': {'net': 'GND', 'pintype': 'power_in', 'pinfunction': 'VCC_1'}})

        self.assertIn('    pin1(VCC,pwr)=GND !!\n', review)

    def test_power_pins_come_before_the_other_pins(self):
        # PWR: が無くなった分、並び順が電源ピンを示す唯一の手がかりになる
        review = self._review(
            {'1': {'net': '/SW', 'pintype': 'output', 'pinfunction': 'SW_1'},
             '2': {'net': 'GND', 'pintype': 'power_in', 'pinfunction': 'GND_2'},
             '3': {'net': 'Net-(X1-FB)', 'pintype': 'input', 'pinfunction': 'FB_3'}})
        section = review[review.index('== INTERNAL COMPONENTS =='):]

        self.assertIn('    pin2(GND,pwr)=GND\n'
                      '    pin1(SW,out)=/SW\n'
                      '    pin3(FB,in)=Net-(X1-FB)\n', section)


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
            {'1': {'net': 'VBUS', 'pintype': 'passive', 'pinfunction': 'Pin_1_1'}})

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
        # 凡例にも pin1 の記述があるので、部品一覧の中だけで前後を見る
        section = review[review.index('== INTERNAL COMPONENTS =='):]

        self.assertLess(section.index('Footprint:'), section.index('pin1'))

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


class HeaderLineWrapTests(unittest.TestCase):
    """説明文は 1 センテンス（1 段落）を論理 1 行で書く。

    折り返しのための改行と、継続行の深いインデントは何の情報も持たないので、
    トークンを無駄にするだけ。部品・ピン行の 2/4 スペースは階層を表すので別。
    """

    def test_pin_annotation_rule_is_documented_outside_a_single_section(self):
        """省略規則は両セクション共通なので、片方の節の説明に埋めない。"""
        review = erc_export.format_review(
            'board.kicad_sch', {}, {}, {}, set(), [])
        header = review[:review.index('== AI REVIEW GUIDANCE ==')]
        rule_line = next(l for l in header.splitlines() if 'pin1=' in l)

        self.assertFalse(rule_line.lstrip().startswith('EXTERNAL INTERFACES'))
        self.assertIn('passive', rule_line)

    def test_header_has_no_wrap_continuation_lines(self):
        review = erc_export.format_review(
            'board.kicad_sch', {}, {}, {}, set(), [])
        header = review[:review.index('== ANOMALIES ==')]

        wrapped = [l for l in header.splitlines() if l.startswith('      ')]

        self.assertEqual(wrapped, [])


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
    # KiCad 10 のネットリストは pinfunction を「ピン名_ピン番号」で出す。
    # シンボル側のピン名は RX / VCC でも、ネットリストでは RX_1 / VCC_3 になる。
    GROVE_PINS = {
        '1': {'net': 'Net-(J1-RX)', 'pintype': 'input', 'pinfunction': 'RX_1'},
        '2': {'net': 'Net-(J1-TX)', 'pintype': 'output', 'pinfunction': 'TX_2'},
        '3': {'net': 'VBUS', 'pintype': 'power_in', 'pinfunction': 'VCC_3'},
        '4': {'net': 'GND', 'pintype': 'power_in', 'pinfunction': 'GND_4'},
    }
    HEADER = {
        'value': 'Conn_01x10', 'libpart': 'Conn_01x10', 'lib': 'Connector_Generic',
        'description': 'Generic connector, single row, 01x10, script generated',
        'sheet': '/',
        'footprint': 'Connector_PinHeader_2.54mm:PinHeader_1x10_P2.54mm_Vertical',
        'manufacturer': '', 'manufacturer_pn': '',
    }
    HEADER_PINS = {
        '1': {'net': 'VBUS', 'pintype': 'passive', 'pinfunction': 'Pin_1_1'},
        '2': {'net': '/DCDC_IN', 'pintype': 'passive', 'pinfunction': 'Pin_2_2'},
        '10': {'net': '/SW', 'pintype': 'passive', 'pinfunction': 'Pin_10_10'},
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
                             'pinfunction': 'VCC_3'}})

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


class DesignNotesParseTests(unittest.TestCase):
    """回路図テキストの設計メモ。部品単位の Purpose では長すぎる記述や、
    回路全体にかかる前提を書くためのもの。

    見出しは "Design Notes" / "設計メモ" / "設計ノート" のいずれかで、
    直後はコロン（半角・全角）か改行。見出しの無いテキストは拾わない
    （位置で部品に紐づく注記なので、位置を落として並べても意味を成さない）。
    """

    def test_text_box_with_a_heading_line(self):
        # 実データの形。KiCad は改行を \\n でエスケープして 1 行に格納する
        sch = ('(text_box "設計メモ\\nJ1 RX/TXはFPGAの3.3V IOと接続'
               '\\nJ1 VCCの想定範囲は4.5-5.5V"\n\t(at 31.75 86.36 0)\n)')

        self.assertEqual(
            erc_export.parse_design_notes(sch),
            ['J1 RX/TXはFPGAの3.3V IOと接続\nJ1 VCCの想定範囲は4.5-5.5V'])

    def test_plain_text_element_is_also_read(self):
        sch = '(text "設計メモ\\n全体は3.3V系"\n\t(at 10 10 0)\n)'

        self.assertEqual(erc_export.parse_design_notes(sch), ['全体は3.3V系'])

    def test_accepts_every_heading_and_colon_form(self):
        for heading in ('Design Notes:', 'design notes:', 'DESIGN NOTES：',
                        '設計メモ:', '設計メモ：', '設計ノート:', '設計ノート：',
                        'Design Notes\\n', '設計メモ\\n', '設計ノート\\n'):
            with self.subTest(heading=heading):
                sch = f'(text_box "{heading}全体は3.3V系"\n)'

                self.assertEqual(erc_export.parse_design_notes(sch),
                                 ['全体は3.3V系'])

    def test_text_without_a_heading_is_ignored(self):
        sch = '(text "1uFでも可"\n)\n(text "RX/TXはホスト基準"\n)'

        self.assertEqual(erc_export.parse_design_notes(sch), [])

    def test_heading_word_must_be_followed_by_a_colon_or_newline(self):
        # 「設計メモは…」のような通常の文は見出しではない
        sch = '(text "設計メモは別紙にある"\n)'

        self.assertEqual(erc_export.parse_design_notes(sch), [])

    def test_empty_note_is_ignored(self):
        sch = '(text_box "設計メモ:"\n)\n(text_box "設計メモ\\n   "\n)'

        self.assertEqual(erc_export.parse_design_notes(sch), [])

    def test_multiple_notes_keep_their_order(self):
        sch = ('(text_box "設計メモ\\n1つ目"\n)\n'
               '(text "Design Notes: 2つ目"\n)')

        self.assertEqual(erc_export.parse_design_notes(sch), ['1つ目', '2つ目'])

    def test_escaped_quotes_and_backslashes_are_restored(self):
        sch = '(text_box "設計メモ\\nU1の\\"EN\\"はH固定 C:\\\\\\\\doc参照"\n)'

        self.assertEqual(erc_export.parse_design_notes(sch),
                         ['U1の"EN"はH固定 C:\\\\doc参照'])


class DesignNotesSourceTests(unittest.TestCase):
    """回路図ファイルが読めなかったとき、メモは黙って 0 件にならないこと。

    読めない原因（パスのずれ、権限）は回路図側の書き忘れと区別できない。
    区別できないまま 0 件になると、書いたはずのメモが消えたことに気づけない。
    """

    NETLIST = """<export>
  <design><source>{sch}</source>
    <sheet number="1" name="/"><title_block><source>root.kicad_sch</source>
      </title_block></sheet>
    <sheet number="2" name="/sub/"><title_block><source>sub.kicad_sch</source>
      </title_block></sheet>
  </design>
</export>"""

    def test_reads_notes_from_every_sheet_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / 'root.kicad_sch').write_text(
                '(text_box "設計メモ\\n親シートのメモ")', encoding='utf-8')
            (tmp_path / 'sub.kicad_sch').write_text(
                '(text_box "設計メモ\\n子シートのメモ")', encoding='utf-8')
            root = ET.fromstring(
                self.NETLIST.format(sch=tmp_path / 'root.kicad_sch'))

            notes = erc_export.read_design_notes(
                root, str(tmp_path / 'root.kicad_sch'))

        self.assertEqual(notes, ['親シートのメモ', '子シートのメモ'])

    def test_unreadable_sheet_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            root = ET.fromstring(
                self.NETLIST.format(sch=tmp_path / 'root.kicad_sch'))
            failed = []

            notes = erc_export.read_design_notes(
                root, str(tmp_path / 'root.kicad_sch'),
                on_error=lambda path, exc: failed.append(path))

        self.assertEqual(notes, [])
        self.assertEqual(len(failed), 2)
        self.assertTrue(failed[0].endswith('root.kicad_sch'))


class DesignNotesFormattingTests(unittest.TestCase):
    NOTES = ['J1 RX/TXはFPGAの3.3V IOと接続\nJ1 VCCの想定範囲は4.5-5.5V']

    def _review(self, notes):
        return erc_export.format_review(
            'board.kicad_sch', {}, {}, {}, set(), [], design_notes=notes)

    def test_notes_are_written_verbatim_line_by_line(self):
        review = self._review(self.NOTES)

        self.assertIn('== DESIGN NOTES ==\n'
                      'J1 RX/TXはFPGAの3.3V IOと接続\n'
                      'J1 VCCの想定範囲は4.5-5.5V\n', review)

    def test_notes_come_before_the_generated_data(self):
        # 人間が書いた唯一の記述なので、機械生成データより先に読ませる
        review = self._review(self.NOTES)

        self.assertLess(review.index('== DESIGN NOTES =='),
                        review.index('== ANOMALIES =='))

    def test_blocks_are_separated_by_a_blank_line(self):
        review = self._review(['1つ目', '2つ目'])

        self.assertIn('== DESIGN NOTES ==\n1つ目\n\n2つ目\n', review)

    def test_section_is_absent_when_there_are_no_notes(self):
        # 凡例はセクションの説明を常に持つので、見出しの有無で判定する
        for notes in (None, []):
            with self.subTest(notes=notes):
                self.assertNotIn('== DESIGN NOTES ==', self._review(notes))


class MainTests(unittest.TestCase):
    def test_design_note_in_the_schematic_reaches_the_review(self):
        """回路図テキスト → review.txt の経路。ここが切れるとメモが黙って消える。"""
        netlist = """<export>
  <design><source>{source}</source>
    <sheet number="1" name="/"><title_block><source>board.kicad_sch</source>
      </title_block></sheet>
  </design>
  <components>
    <comp ref="R1"><value>10k</value><libsource lib="Device" part="R"/>
      <sheetpath names="/"/></comp>
  </components>
  <nets><net name="SIG"><node ref="R1" pin="1" pintype="passive"/></net></nets>
</export>"""
        sch = ('(kicad_sch\n'
               '\t(text_box "設計メモ\\nJ1 VCCの想定範囲は4.5-5.5V"\n\t\t(at 1 2 0)\n\t)\n'
               '\t(text "1uFでも可"\n\t\t(at 3 4 0)\n\t)\n)')

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source = tmp_path / 'board.kicad_sch'
            source.write_text(sch, encoding='utf-8')
            input_net = tmp_path / 'input.net'
            input_net.write_text(netlist.format(source=source), encoding='utf-8')

            with (
                mock.patch.object(erc_export, '_FALLBACK_LOG', str(tmp_path / 'error.log')),
                mock.patch.object(erc_export, 'find_kicad_cli', return_value='kicad-cli'),
                mock.patch.object(
                    erc_export, '_run_cmd_with_progress',
                    return_value=subprocess.CompletedProcess([], -1, None, 'cancelled'),
                ),
                mock.patch.object(sys, 'argv',
                                  ['erc_export.py', '--review-only', str(input_net)]),
            ):
                erc_export.main()

            review = next((tmp_path / 'ai-review').glob('*.review.txt')).read_text(
                encoding='utf-8')

        self.assertIn('== DESIGN NOTES ==\nJ1 VCCの想定範囲は4.5-5.5V\n', review)
        self.assertNotIn('1uFでも可', review)

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
