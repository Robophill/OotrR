import _src_path  # noqa: F401  (src on sys.path)
import copy
import io
import struct
import unittest
from unittest.mock import Mock

import zstandard as zstd
from evfl import EventFlow
from evfl.actor import Actor
from evfl.common import ActorIdentifier, StringHolder
from evfl.container import Container
from evfl.event import ActionEvent, Event
from evfl.flowchart import Flowchart
from evfl.timeline import Timeline
from evfl.util import make_rindex
from oot_rando.formats import byml_codec
from oot_rando.formats.evfl_tools import parse_flow
from oot_rando.formats.inspect_resources import Byml, sarc
from oot_rando.game.item_forms import canonical_of, replacement_bundle_actor
from oot_rando.patching.item_replacements import (ECONOMY_PRESETS, apply_resource_replacements, apply_to_generation,
                                                   economy_preset_tooltip, economy_replacements,
                                                   _DROP_GLOBAL, _DropGlobalByml, _patch_drop_pack,
                                                   _patch_environment_drops, _patch_global_drop_substitutions,
                                                   patch_event_items, patch_map_items,
                                                   replace_planned_shop_rewards, ReplacementCycle,
                                                   replacement_actor, replacement_item,
                                                   update_spoiler, update_tracker)


class ItemReplacementTests(unittest.TestCase):
    def global_drop_fixture(self):
        green = 'Work/Actor/Obj_Rupee_Green.engine__actor__ActorParam.gyml'
        heart = 'Work/Actor/Obj_Heart.engine__actor__ActorParam.gyml'
        root = {'DropLotteryMasterData': {
            'AreaUniqueRatio': byml_codec.U32(123),
            'ReplaceSetting': [
                {'TargetActor': heart, 'ReplaceActor': [{'ReplaceActor': green, 'Probability': 100}]},
                {'TargetActor': 'Work/Actor/Obj_Bomb.engine__actor__ActorParam.gyml',
                 'ReplaceActor': [{'ReplaceActor': green, 'Probability': 100}]},
                {'TargetActor': 'Work/Actor/Obj_Fairy.engine__actor__ActorParam.gyml',
                 'ReplaceActor': [{'ReplaceActor': heart, 'Probability': 100}]},
            ]}}
        raw = bytearray(byml_codec.dump({'endian': '<', 'version': 7, 'root': root}))
        from oot_rando.formats.byml_writer import _dict_entry
        byml = Byml(raw)
        _, master = _dict_entry(byml, byml.u32(12), 'DropLotteryMasterData')
        _, opaque = _dict_entry(byml, byml.u32(master), 'AreaUniqueRatio')
        raw[opaque - 1] = 0x20
        return bytes(raw)

    def test_economy_presets_define_labels_descriptions_and_replacements(self):
        self.assertTrue(ECONOMY_PRESETS)
        for key, preset in ECONOMY_PRESETS.items():
            with self.subTest(preset=key):
                self.assertTrue(preset.label)
                self.assertTrue(preset.description)
                self.assertIs(economy_replacements(key), preset.replacements)

    def test_economy_tooltip_summarizes_setting_and_lists_every_preset(self):
        tooltip = economy_preset_tooltip()
        title, _, body = tooltip.partition('\n')

        self.assertEqual(title, 'Rupee Mania!')
        self.assertTrue(body.startswith('Choose how rupee rewards are changed by the selected preset.'))
        for preset in ECONOMY_PRESETS.values():
            self.assertIn(f'• {preset.label}: {preset.description}', body)

    def test_replacement_targets_must_be_non_empty_names(self):
        with self.assertRaises(ValueError):
            replacement_item('Rupee (1)', {'Rupee (1)': []})
        with self.assertRaises(ValueError):
            replacement_item('Rupee (1)', {'Rupee (1)': ['Rupees (5)', '']})
        with self.assertRaises(TypeError):
            replacement_item('Rupee (1)', {'Rupee (1)': 200})

    def test_map_rewards_change_without_touching_other_actor_fields(self):
        raw = byml_codec.dump({'endian': '<', 'version': 7, 'root': {'Actors': [
            {'Gyaml': 'Obj_Rupee_Green',
             'Dynamic': {'Drop__DropActor': 'TBox_Rupee_Green',
                         'Drop__CheckActor': 'TBox_Rupee_Green',
                         'Drop__DropActor_Substitute': None}},
            {'Gyaml': 'Obj_Rupee_Green_GS'},
            {'Gyaml': 'Obj_Heart'},
        ]}})

        derived = set()
        patched, count = patch_map_items(
            raw, 'Banc/KokiriForest/Map.bcett.byml.zs', {'Rupee (1)': 'Rupees (200)'}, derived)

        actors = Byml(patched).root['Actors']
        self.assertEqual(count, 3)
        self.assertEqual(actors[0]['Gyaml'], 'Obj_Rupee_Gold')
        self.assertEqual(actors[0]['Dynamic']['Drop__DropActor'], 'TBox_Rupee_Gold')
        self.assertEqual(actors[0]['Dynamic']['Drop__CheckActor'], 'TBox_Rupee_Green')
        self.assertEqual(actors[1]['Gyaml'], 'Obj_Rupee_Gold_GS')
        self.assertEqual(actors[2]['Gyaml'], 'Obj_Heart')
        self.assertEqual(derived, {'Obj_Rupee_Gold_GS'})

    def test_randomized_shop_reward_is_retargeted_before_pack_generation(self):
        plan = {
            'placements': [{'id': 'shop#1', 'item': 'Rupee (1)', 'folder': 'Market',
                            'form': 'shop', 'after': 'Shop_Rando_TBox_Rupee_Green'}],
            'spoiler': [{'id': 'shop#1', 'item': 'Rupee (1)', 'ap_item': 'Rupee (1)',
                         'actor': 'Shop_Rando_TBox_Rupee_Green'}],
        }

        changed = replace_planned_shop_rewards(plan, {'Rupee (1)': 'Rupees (200)'})

        self.assertEqual(changed, 1)
        self.assertEqual(plan['placements'][0]['item'], 'Rupees (200)')
        self.assertEqual(plan['placements'][0]['after'], 'Shop_Rando_TBox_Rupee_Gold')
        self.assertEqual(plan['spoiler'][0]['ap_item'], 'Rupees (200)')

    def test_randomized_environment_reward_is_retargeted_before_pack_generation(self):
        plan = {
            'placements': [{'id': 'HyruleField#rocks#1', 'item': 'Rupee (1)',
                            'folder': 'HyruleField', 'form': 'world', 'source': 'rocks',
                            'after': 'Obj_Rupee_Green'}],
            'spoiler': [{'id': 'HyruleField#rocks#1', 'item': 'Rupee (1)',
                         'ap_item': 'Rupee (1)', 'actor': 'Obj_Rupee_Green'}],
        }

        changed = replace_planned_shop_rewards(plan, {'Rupee (1)': 'Deku Nuts (5)'})

        self.assertEqual(changed, 1)
        self.assertEqual(plan['placements'][0]['item'], 'Deku Nuts (5)')
        expected = replacement_bundle_actor('TBox_DekuNutBundle', 'world')
        self.assertEqual(plan['placements'][0]['after'], expected)
        self.assertEqual(plan['spoiler'][0]['actor'], expected)

    def test_ammo_replacements_keep_bundle_identity_in_world_and_drops(self):
        for item, canonical in (('Deku Nuts (5)', 'TBox_DekuNutBundle'),
                                ('Bombs (5)', 'Item_BombBundle_Small'),
                                ('Bombchus (5)', 'Item_BombchuBundle_Small'),
                                ('Arrows (5)', 'TBox_ArrowBundle_Small'),
                                ('Deku Seeds (30)', 'TBox_DekuSeedBundle')):
            with self.subTest(item=item):
                rules = {'Rupee (1)': item}
                for form in ('world', 'drop'):
                    actor = replacement_actor('Obj_Rupee_Green', '', rules, form=form)
                    self.assertEqual(actor, replacement_bundle_actor(canonical, form))
                    self.assertEqual(canonical_of(actor), canonical)
                self.assertEqual(replacement_actor('TBox_Rupee_Green', '', rules, form='chest'), canonical)
        self.assertEqual(replacement_actor('Obj_Rupee_Green', '', {'Rupee (1)': 'Deku Stick (1)'},
                                           form='drop'), 'Obj_DekuStick')

    def test_environment_drop_tables_are_replaced_inside_actor_packs(self):
        table = 'DropTable/DropSubTable.bgyml'
        pack = byml_codec.build_sarc({
            table: byml_codec.dump({'endian': '<', 'version': 7, 'root': {'Items': [{
                'DropActorName': 'Work/Actor/Obj_Rupee_Green.engine__actor__ActorParam.gyml',
                'Type': byml_codec.U32(995674644),
                'DropProbability': 100,
            }]}}),
        })

        patched, count, derived = _patch_drop_pack(
            pack, 'Pack/Actor/Obj_LiftRock_A_01.pack.zs',
            {'Rupee (1)': 'Deku Nuts (5)'}, ReplacementCycle({'Rupee (1)': 'Deku Nuts (5)'}))

        item = byml_codec.load(sarc(patched)[table])['root']['Items'][0]
        self.assertEqual(count, 1)
        expected = replacement_bundle_actor('TBox_DekuNutBundle', 'drop')
        self.assertEqual(item['DropActorName'],
                         f'Work/Actor/{expected}.engine__actor__ActorParam.gyml')
        self.assertEqual(int(item['Type']), 126134492)
        self.assertEqual(derived, {expected})

    def test_environment_map_drops_use_falling_pickups_not_standing_pickups(self):
        raw = byml_codec.dump({'endian': '<', 'version': 7, 'root': {'Actors': [
            {'Gyaml': 'Obj_LiftRock_A_01', 'Dynamic': {'Drop__DropActor': 'Obj_Rupee_Green'}},
            {'Gyaml': 'Obj_Tree_A_01', 'Dynamic': {'Drop__DropActor': 'Obj_Rupee_Green'}},
            {'Gyaml': 'Obj_Rupee_Green'},
            {'Gyaml': 'Obj_TBox_A_01', 'Dynamic': {'Drop__DropActor': 'TBox_Rupee_Green'}},
        ]}})
        derived = set()

        patched, count = patch_map_items(
            raw, 'Banc/KokiriForest/Map.bcett.byml.zs', {'Rupee (1)': 'Bottle with Milk'}, derived)

        actors = Byml(patched).root['Actors']
        self.assertEqual(count, 4)
        self.assertEqual(actors[0]['Dynamic']['Drop__DropActor'], 'Obj_RandoDrop_Item_Bottle_Milk')
        self.assertEqual(actors[1]['Dynamic']['Drop__DropActor'], 'Obj_RandoDrop_Item_Bottle_Milk')
        self.assertEqual(actors[2]['Gyaml'], 'Obj_Rando_Item_Bottle_Milk')
        self.assertEqual(actors[3]['Dynamic']['Drop__DropActor'], 'Item_Bottle_Milk')
        self.assertEqual(derived, {'Obj_RandoDrop_Item_Bottle_Milk', 'Obj_Rando_Item_Bottle_Milk'})

    def test_environment_drop_pack_uses_falling_pickup_for_non_native_item(self):
        member = 'DropTable/DropSubTable.bgyml'
        pack = byml_codec.build_sarc({
            member: byml_codec.dump({'endian': '<', 'version': 7, 'root': {'Items': [{
                'DropActorName': 'Work/Actor/Obj_Rupee_Green.engine__actor__ActorParam.gyml',
                'Type': byml_codec.U32(995674644),
            }]}}),
        })
        replacements = {'Rupee (1)': 'Bottle with Milk'}
        patched, count, derived = _patch_drop_pack(
            pack, 'Pack/Actor/Obj_BrokenRock_A_01.pack.zs', replacements, ReplacementCycle(replacements))

        item = byml_codec.load(sarc(patched)[member])['root']['Items'][0]
        self.assertEqual(item['DropActorName'],
                         'Work/Actor/Obj_RandoDrop_Item_Bottle_Milk.engine__actor__ActorParam.gyml')
        self.assertEqual((count, derived), (1, {'Obj_RandoDrop_Item_Bottle_Milk'}))

    def test_environment_drop_scan_ignores_unrelated_byml_members(self):
        table = 'DropTable/DropSubTable.bgyml'
        unrelated = bytearray(byml_codec.dump({
            'endian': '<', 'version': 7, 'root': {'Other': 1}}))
        root = int.from_bytes(unrelated[12:16], 'little')
        unrelated[root + 7] = 0x20
        pack = byml_codec.build_sarc({
            'Component/Other/Unrelated.bgyml': bytes(unrelated),
            table: byml_codec.dump({'endian': '<', 'version': 7, 'root': {'Items': [{
                'DropActorName': 'Work/Actor/Obj_Rupee_Green.engine__actor__ActorParam.gyml',
                'Type': byml_codec.U32(995674644),
                'DropProbability': 100,
            }]}}),
        })

        patched, count, derived = _patch_drop_pack(
            pack, 'Pack/Actor/Obj_LiftRock_A_01.pack.zs',
            {'Rupee (1)': 'Deku Nuts (5)'}, ReplacementCycle({'Rupee (1)': 'Deku Nuts (5)'}))

        files = sarc(patched)
        self.assertEqual(count, 1)
        self.assertEqual(files['Component/Other/Unrelated.bgyml'], bytes(unrelated))
        self.assertEqual(derived, {replacement_bundle_actor('TBox_DekuNutBundle', 'drop')})

    def test_relevant_drop_byml_error_identifies_pack_and_member(self):
        member = 'DropTable/DropSubTable.bgyml'
        raw = bytearray(byml_codec.dump({
            'endian': '<', 'version': 7, 'root': {'DropActorName': 'something'}}))
        root = int.from_bytes(raw[12:16], 'little')
        raw[root + 7] = 0x20
        pack = byml_codec.build_sarc({member: bytes(raw)})

        with self.assertRaisesRegex(
                ValueError, r'Pack/Actor/Test\.pack\.zs:DropTable/DropSubTable\.bgyml:'
                r' Unsupported BYML type 0x20'):
            _patch_drop_pack(
                pack, 'Pack/Actor/Test.pack.zs',
                {'Rupee (1)': 'Deku Nuts (5)'}, ReplacementCycle({'Rupee (1)': 'Deku Nuts (5)'}))

    def test_environment_pack_is_updated_as_an_existing_resource(self):
        path = 'Pack/Actor/Obj_BrokenBox_A_01.pack.zs'
        member = 'DropTable/DropSubTable.bgyml'
        raw = byml_codec.build_sarc({
            member: byml_codec.dump({'endian': '<', 'version': 7, 'root': {'Items': [{
                'DropActorName': 'Work/Actor/Obj_Rupee_Green.engine__actor__ActorParam.gyml',
                'Type': byml_codec.U32(995674644),
                'DropProbability': 100,
            }]}}),
        })
        compressed = zstd.ZstdCompressor().compress(raw)
        extra_raw = {}

        stats, derived = _patch_environment_drops(
            {path: path}, {'Obj_BrokenBox_A_01'}, extra_raw, {}, {}, lambda _: compressed,
            {'Rupee (1)': 'Deku Nuts (5)'}, ReplacementCycle({'Rupee (1)': 'Deku Nuts (5)'}))

        self.assertEqual(stats, {'drop_table_items': 1, 'drop_packs': 1})
        self.assertIn(path, extra_raw)
        self.assertNotIsInstance(extra_raw[path], tuple)
        self.assertEqual(derived, {replacement_bundle_actor('TBox_DekuNutBundle', 'drop')})

    def test_environment_drop_scan_updates_generated_repeatable_pack(self):
        source = 'Pack/Actor/Obj_BrokenRock_A_01.pack.zs'
        generated = 'Pack/Actor/Obj_RandoOnce_123.pack.zs'
        template = 'Pack/Actor/Obj_Token_Gold.pack.zs'
        member = 'DropTable/DropSubTable.bgyml'
        raw = byml_codec.build_sarc({
            member: byml_codec.dump({'endian': '<', 'version': 7, 'root': {'Items': [{
                'DropActorName': 'Work/Actor/Obj_Rupee_Green.engine__actor__ActorParam.gyml',
                'Type': byml_codec.U32(995674644),
                'DropProbability': 100,
            }]}}),
        })
        new_files = {generated: (raw, template)}
        extra_raw = {}

        stats, derived = _patch_environment_drops(
            {}, {'Obj_BrokenRock_A_01'}, extra_raw, new_files, {}, lambda _: None,
            {'Rupee (1)': 'Deku Nuts (5)'}, ReplacementCycle({'Rupee (1)': 'Deku Nuts (5)'}))

        self.assertEqual(stats, {'drop_table_items': 1, 'drop_packs': 1})
        self.assertEqual(new_files[generated][1], template)
        item = byml_codec.load(sarc(new_files[generated][0])[member])['root']['Items'][0]
        expected = replacement_bundle_actor('TBox_DekuNutBundle', 'drop')
        self.assertEqual(item['DropActorName'],
                         f'Work/Actor/{expected}.engine__actor__ActorParam.gyml')
        self.assertNotIn(source, extra_raw)
        self.assertEqual(derived, {expected})

    def test_global_drop_substitutions_preserve_lottery_and_cycle_shared_entries(self):
        raw = self.global_drop_fixture()
        original = _DropGlobalByml(raw).root
        patched, count, derived = _patch_global_drop_substitutions(
            raw, {'Rupee (1)': ('Bombs (5)', 'Deku Nuts (5)')})

        expected = copy.deepcopy(original)
        entries = expected['DropLotteryMasterData']['ReplaceSetting']
        bomb = replacement_bundle_actor('Item_BombBundle_Small', 'drop')
        nut = replacement_bundle_actor('TBox_DekuNutBundle', 'drop')
        entries[0]['ReplaceActor'][0]['ReplaceActor'] = f'Work/Actor/{bomb}.engine__actor__ActorParam.gyml'
        entries[1]['ReplaceActor'][0]['ReplaceActor'] = f'Work/Actor/{nut}.engine__actor__ActorParam.gyml'
        self.assertEqual(_DropGlobalByml(patched).root, expected)
        self.assertEqual(count, 2)
        self.assertEqual(derived, {bomb, nut})
        unchanged, count, _ = _patch_global_drop_substitutions(raw, {'Rupees (20)': 'Rupees (200)'})
        self.assertEqual((unchanged, count), (raw, 0))

    def test_global_drop_substitution_registers_derived_pickups(self):
        patched, count, derived = _patch_global_drop_substitutions(
            self.global_drop_fixture(), {'Rupee (1)': 'Master Sword'})
        actor = 'Obj_RandoDrop_Item_Rando_MasterSword'
        entries = _DropGlobalByml(patched).root['DropLotteryMasterData']['ReplaceSetting']
        self.assertIn(actor, entries[0]['ReplaceActor'][0]['ReplaceActor'])
        self.assertEqual((count, derived), (2, {actor}))

    def test_global_fallback_uses_falling_pickup_for_non_native_item(self):
        patched, count, derived = _patch_global_drop_substitutions(
            self.global_drop_fixture(), {'Rupee (1)': 'Bottle with Milk'})
        entries = _DropGlobalByml(patched).root['DropLotteryMasterData']['ReplaceSetting']
        self.assertEqual(entries[0]['ReplaceActor'][0]['ReplaceActor'],
                         'Work/Actor/Obj_RandoDrop_Item_Bottle_Milk.engine__actor__ActorParam.gyml')
        self.assertEqual((count, derived), (2, {'Obj_RandoDrop_Item_Bottle_Milk'}))

    def test_environment_spoiler_reports_the_falling_pickup(self):
        gid = 'KokiriForest#rocks#1'
        spoiler = {'placements': [{'id': gid, 'item': 'Rupee (1)', 'after': 'Obj_Rupee_Green'}]}
        world = {'placements': [{'id': gid, 'category': 'rocks', 'after': 'Obj_Rupee_Green'}]}
        stats = {'configured': {'Rupee (1)': 'Bottle with Milk'}}

        update_spoiler(spoiler, None, world, [], stats)

        self.assertEqual(spoiler['placements'][0]['item'], 'Bottle with Milk')
        self.assertEqual(spoiler['placements'][0]['after'], 'Obj_RandoDrop_Item_Bottle_Milk')

    def test_generation_updates_fallback_for_unshuffled_rock(self):
        map_path = 'Banc/KokiriForest/KokiriForest_Child_Static.bcett.byml.zs'
        pack_path = 'Pack/Actor/Obj_LiftRock_A_01.pack.zs'
        rock = {'Gyaml': 'Obj_LiftRock_A_01', 'Hash': byml_codec.U64(1)}
        map_raw = byml_codec.dump({'endian': '<', 'version': 7, 'root': {'Actors': [rock]}})
        pack_raw = byml_codec.build_sarc({'unrelated': b'unchanged'})
        compressor = zstd.ZstdCompressor()
        data = {map_path: compressor.compress(map_raw), pack_path: compressor.compress(pack_raw),
                _DROP_GLOBAL: self.global_drop_fixture()}
        members = {map_path: map_path, pack_path: pack_path, _DROP_GLOBAL: _DROP_GLOBAL}
        nup = Mock()
        nup.read.side_effect = data.__getitem__
        raw_maps, extra_raw, plain_raw = {}, {}, {}

        stats = apply_to_generation(nup, members, raw_maps, extra_raw, plain_raw, {}, {},
                                    data.__getitem__, lambda _: None, economy_preset='gold_rush')

        self.assertEqual(stats['drop_substitutions'], 2)
        self.assertEqual(raw_maps, {})
        self.assertEqual(extra_raw, {})
        entries = _DropGlobalByml(plain_raw[_DROP_GLOBAL]).root['DropLotteryMasterData']['ReplaceSetting']
        self.assertTrue(all('Obj_Rupee_Gold' in entry['ReplaceActor'][0]['ReplaceActor']
                            for entry in entries[:2]))
        self.assertIn('Obj_Heart', entries[2]['ReplaceActor'][0]['ReplaceActor'])

    def test_resource_scan_patches_an_unmodified_vanilla_map(self):
        path = 'Banc/KokiriForest/Map.bcett.byml.zs'
        raw = byml_codec.dump({'endian': '<', 'version': 7, 'root': {
            'Actors': [{'Gyaml': actor} for actor in (
                'Obj_Rupee_Green', 'Obj_Rupee_Blue', 'Obj_Rupee_Red', 'Obj_Rupee_Purple')]}})
        compressed = zstd.ZstdCompressor().compress(raw)
        raw_maps, extra_raw = {}, {}

        counts = apply_resource_replacements(
            {path: path}, raw_maps, extra_raw, {}, lambda _: compressed,
            {'Rupee*': 'Rupees (200)'})

        actors = Byml(raw_maps[path]).root['Actors']
        self.assertEqual(counts['map_fields'], 4)
        self.assertEqual([actor['Gyaml'] for actor in actors], ['Obj_Rupee_Gold'] * 4)

    def test_event_reward_parameter_is_replaced(self):
        flow = EventFlow()
        flow.flowchart = Flowchart()
        flow.flowchart.name = 'ReplacementTest'
        actor = Actor()
        actor.identifier = ActorIdentifier('GameDataActor')
        actor.concurrent_clips = 1
        actor.actions.append(StringHolder('EventTriggerGive'))
        flow.flowchart.actors.append(actor)
        event = Event()
        event.name = 'GiveItem'
        event.data = ActionEvent()
        event.data.actor = make_rindex(actor)
        event.data.actor_action = make_rindex(actor.actions[0])
        event.data.params = Container()
        event.data.params.data = {'ActorName': 'TBox_Rupee_Green', 'Unrelated': 'TBox_Rupee_Green'}
        flow.flowchart.events.append(event)
        stream = io.BytesIO()
        flow.write(stream)

        patched, count = patch_event_items(stream.getvalue(), {'Rupee (1)': 'Rupees (200)'})

        params = parse_flow(patched).flowchart.events[0].data.params.data
        self.assertEqual(count, 1)
        self.assertEqual(params['ActorName'], 'TBox_Rupee_Gold')
        self.assertEqual(params['Unrelated'], 'TBox_Rupee_Green')

    def test_timeline_only_cutscene_with_matching_model_is_left_unchanged(self):
        flow = EventFlow()
        flow.timeline = Timeline()
        flow.timeline.name = 'BossCutscene'
        actor = Actor()
        actor.identifier = ActorIdentifier('HeartModel')
        actor.concurrent_clips = 1
        actor.params = Container()
        actor.params.data = {'UseEventModelAnime': 'Dm_BossOut_Obj_Heart_Container'}
        flow.timeline.actors.append(actor)
        stream = io.BytesIO()
        self.assertTrue(flow.write(stream))
        raw = stream.getvalue()
        path = 'Event/EventFlow/BossCutscene.bfevfl.zs'
        extra_raw = {}

        counts = apply_resource_replacements(
            {path: path}, {}, extra_raw, {}, lambda _: zstd.ZstdCompressor().compress(raw),
            {'Recovery Heart': 'Deku Stick (1)'})

        self.assertEqual(counts['event_parameters'], 0)
        self.assertEqual(extra_raw, {})
        self.assertEqual(patch_event_items(raw, {'Recovery Heart': 'Deku Stick (1)'}), (raw, 0))

    def test_event_without_flowchart_or_timeline_is_rejected(self):
        flow = EventFlow()
        flow.timeline = Timeline()
        flow.timeline.name = 'Empty'
        stream = io.BytesIO()
        self.assertTrue(flow.write(stream))
        raw = bytearray(stream.getvalue())
        struct.pack_into('<HH', raw, 0x20, 0, 0)

        with self.assertRaisesRegex(ValueError, 'neither a flowchart nor a timeline'):
            patch_event_items(bytes(raw), {'Recovery Heart': 'Deku Stick (1)'}, 'empty.bfevfl')

    def test_replacement_actor_preserves_event_form(self):
        result = replacement_actor('TBox_Rupee_Green', '', {'Rupee (1)': 'Rupees (200)'}, form='event')
        self.assertEqual(result, 'TBox_Rupee_Gold')
        self.assertEqual(canonical_of('Obj_Rando_TBox_Rupee_Gold'), 'TBox_Rupee_Gold')

    def test_replacement_chains_apply_only_one_step_to_original_items(self):
        replacements = {
            'Rupee (1)': 'Rupees (5)',
            'Rupees (5)': 'Rupees (20)',
            'Rupees (20)': 'Rupees (50)',
        }

        self.assertEqual(replacement_actor('TBox_Rupee_Green', '', replacements, form='event'),
                         'TBox_Rupee_Blue')
        self.assertEqual(replacement_actor('TBox_Rupee_Blue', '', replacements, form='event'),
                         'TBox_Rupee_Red')
        self.assertEqual(replacement_actor('TBox_Rupee_Red', '', replacements, form='event'),
                         'TBox_Rupee_Purple')

    def test_tracker_shows_replacements(self):
        resource = 'Banc/KokiriForest/KokiriForest_Child_Dynamic.bcett.byml.zs'
        world = {'placements': [
            {'id': 'KokiriForest#1#Drop', 'hash': '1', 'resources': [resource],
             'folder': 'KokiriForest', 'after': 'Obj_Rupee_Green'},
            {'id': 'KokiriForest#2#Drop', 'hash': '2', 'resources': [resource],
             'folder': 'KokiriForest', 'after': 'Obj_Rupee_Green'},
        ]}
        tracking = {'checks': {
            'KokiriForest#1#Drop': {'item': 'Rupee (1)', 'reward': 'Green Rupee'},
            'KokiriForest#2#Drop': {'item': 'Rupee (1)', 'reward': 'Green Rupee'},
        }}
        stats = {}

        updated = update_tracker(tracking, None, world, [], stats,
                                 replacements={'Rupee*': 'Rupees (200)'})

        self.assertEqual(updated, 2)
        self.assertEqual(tracking['checks']['KokiriForest#1#Drop']['item'], 'Rupees (200)')
        self.assertEqual(tracking['checks']['KokiriForest#1#Drop']['reward'], 'Gold Rupee')
        self.assertEqual(tracking['checks']['KokiriForest#2#Drop']['item'], 'Rupees (200)')
        self.assertEqual(tracking['checks']['KokiriForest#2#Drop']['reward'], 'Gold Rupee')

    def test_full_tracker_uses_original_item_for_replacements(self):
        resource = 'Banc/KokiriForest/KokiriForest_Child_Dynamic.bcett.byml.zs'
        full = {
            'placements': [{'id': 'random#1', 'item': 'Rupee (1)'}],
            'matches': {
                'random#1': {'vanilla': 'Rupee (1)', 'variants': [
                    {'resource': resource, 'index': 0, 'field': 'Gyaml'}]},
                'vanilla#2': {'vanilla': 'Rupee (1)', 'variants': [
                    {'resource': resource, 'index': 1, 'field': 'Gyaml'}]},
            },
        }
        tracking = {'checks': {
            'random#1': {'item': 'Rupee (1)', 'reward': 'Green Rupee'},
            'vanilla#2': {'item': 'Rupee (1)', 'reward': 'Green Rupee'},
        }}
        stats = {}

        updated = update_tracker(tracking, full, {}, [], stats,
                                 replacements={'Rupee*': 'Rupees (5)'})

        self.assertEqual(updated, 2)
        for check in tracking['checks'].values():
            self.assertEqual(check['item'], 'Rupees (5)')
            self.assertEqual(check['reward'], 'Blue Rupee')

    def test_spoiler_updates_world_reward_actors(self):
        resource = 'Banc/KokiriForest/KokiriForest_Child_Dynamic.bcett.byml.zs'
        world = {'placements': [
            {'id': 'KokiriForest#1#Drop', 'hash': '1', 'resources': [resource],
             'folder': 'KokiriForest', 'after': 'Obj_Rupee_Green'},
            {'id': 'KokiriForest#2#Drop', 'hash': '2', 'resources': [resource],
             'folder': 'KokiriForest', 'after': 'Obj_Rupee_Green'},
        ]}
        spoiler = {'placements': [
            {'id': 'KokiriForest#1#Drop', 'after': 'Obj_Rupee_Green'},
            {'id': 'KokiriForest#2#Drop', 'after': 'Obj_Rupee_Green'},
        ]}
        stats = {}

        updated = update_spoiler(spoiler, None, world, [], stats,
                                 replacements={'Rupee*': 'Rupees (200)'})

        self.assertEqual(updated, 2)
        self.assertEqual(spoiler['placements'][0]['after'], 'Obj_Rupee_Gold')
        self.assertEqual(spoiler['placements'][1]['after'], 'Obj_Rupee_Gold')

    def test_full_spoiler_updates_item_and_playthrough_once(self):
        resource = 'Banc/KokiriForest/KokiriForest_Child_Dynamic.bcett.byml.zs'
        full = {
            'placements': [
                {'id': 'random#1', 'folder': 'KokiriForest', 'item': 'Rupee (1)',
                 'after': 'TBox_Rupee_Green'},
                {'id': 'random#2', 'folder': 'KokiriForest', 'item': 'Rupee (1)',
                 'after': 'TBox_Rupee_Green'},
            ],
            'matches': {
                'random#1': {'variants': [{'resource': resource, 'index': 0, 'field': 'Gyaml'}]},
                'random#2': {'variants': [{'resource': resource, 'index': 1, 'field': 'Gyaml'}]},
            },
        }
        spoiler = {
            'placements': [
                {'id': 'random#1', 'location': 'First', 'item': 'Rupee (1)',
                 'ap_item': 'Rupee (1)', 'actor': 'TBox_Rupee_Green'},
                {'id': 'random#2', 'location': 'Second', 'item': 'Rupee (1)',
                 'ap_item': 'Rupee (1)', 'actor': 'TBox_Rupee_Green'},
            ],
            'playthrough': [{'sphere': 0, 'items': ['First : Rupee (1)']}],
        }
        stats = {}

        updated = update_spoiler(spoiler, full, {}, [], stats,
                                 replacements={'Rupee*': 'Rupees (5)'})

        self.assertEqual(updated, 2)
        self.assertEqual(spoiler['placements'][0]['item'], 'Rupees (5)')
        self.assertEqual(spoiler['placements'][0]['ap_item'], 'Rupees (5)')
        self.assertEqual(spoiler['placements'][0]['actor'], 'TBox_Rupee_Blue')
        self.assertEqual(spoiler['placements'][1]['item'], 'Rupees (5)')
        self.assertEqual(spoiler['playthrough'][0]['items'], ['First : Rupees (5)'])

    def test_full_spoiler_does_not_update_the_same_placement_twice(self):
        full = {
            'placements': [
                {'id': 'shared#1', 'folder': 'KokiriForest', 'item': 'Rupee (1)',
                 'after': 'TBox_Rupee_Green'},
            ],
            'matches': {
                'shared#1': {'variants': [
                    {'resource': 'Banc/KokiriForest/Map.bcett.byml.zs',
                     'index': 0, 'field': 'Gyaml'}]},
            },
        }
        world = {'placements': [
            {'id': 'shared#1', 'hash': '1',
             'resources': ['Banc/KokiriForest/Map.bcett.byml.zs'],
             'folder': 'KokiriForest', 'after': 'Obj_Rupee_Green'},
        ]}
        spoiler = {'placements': [
            {'id': 'shared#1', 'location': 'Shared', 'item': 'Rupee (1)',
             'ap_item': 'Rupee (1)', 'actor': 'TBox_Rupee_Green'},
        ]}
        stats = {'configured': {'Rupee (1)': 'Rupees (5)'}, 'assignments': [],
                 'hash_assignments': []}

        updated = update_spoiler(spoiler, full, world, [], stats)

        self.assertEqual(updated, 1)
        self.assertEqual(spoiler['placements'][0]['item'], 'Rupees (5)')
        self.assertEqual(spoiler['placements'][0]['actor'], 'TBox_Rupee_Blue')

    def test_spoiler_does_not_count_an_unchanged_target(self):
        world = {'placements': [
            {'id': 'KokiriForest#1#Drop', 'hash': '1',
             'resources': ['Banc/KokiriForest/Map.bcett.byml.zs'],
             'folder': 'KokiriForest', 'after': 'Obj_Rupee_Green'},
        ]}
        spoiler = {'placements': [
            {'id': 'KokiriForest#1#Drop', 'item': 'Rupee (1)',
             'ap_item': 'Rupee (1)', 'actor': 'Obj_Rupee_Green'},
        ]}
        stats = {'configured': {'Rupee (1)': 'Rupee (1)'}, 'assignments': [],
                 'hash_assignments': []}

        updated = update_spoiler(spoiler, None, world, [], stats)

        self.assertEqual(updated, 0)


if __name__ == '__main__':
    unittest.main()
