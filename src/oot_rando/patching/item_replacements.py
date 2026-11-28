"""Developer-edited, global item reward replacements.

Add economy presets to ``ECONOMY_PRESETS``, with a label, tooltip description,
and replacement rules. A target can be one item name or an ordered tuple of
names, which cycles as matching rewards are patched.

Each reward is matched once by its original item name; replacement chains do not cascade.
If multiple patterns match, the first matching rule in table order wins.
Derived world and shop forms are built when a replacement needs them.
Replacement ammo pickups use the quantity in the item name, independently of
the native pickup's bundle size.

Map and event rewards share a cycle during their resource scan. Planned shop
rewards use a separate cycle because they are retargeted earlier in generation.
Global drop substitutions use their own cycle.
"""
from fnmatch import fnmatchcase
import struct
from typing import NamedTuple

from oot_rando.formats.byml_writer import patch_strings
from oot_rando.formats import byml_codec
from oot_rando.formats.evfl_tools import parse_flow
from oot_rando.formats.inspect_resources import Byml, decompress, sarc
from oot_rando.game.item_forms import (DROP_PREFIX, NATIVE_WORLD, SHOP_PREFIX, WATER_DROP_PREFIX, WATER_PREFIX,
                                      WORLD_CANONICAL, WORLD_PREFIX, canonical_of, form_actor, is_derived,
                                      replacement_bundle_actor)
from oot_rando.game.randomizer_categories import candidate_category
from oot_rando.game.remake_world import ACTOR_ITEMS, actor_item, item_actor
from oot_rando.patching.event_patch import patch_event_params
from oot_rando.patching.repeatable_adapter import REPEATABLE


DEFAULT_ECONOMY_PRESET = 'vanilla'  # Default Economy preset for new seeds.

class EconomyPreset(NamedTuple):
    label: str
    description: str
    replacements: dict[str, str | tuple[str, ...]]


ECONOMY_PRESETS = {
    'vanilla': EconomyPreset('Vanilla', 'Leave all rupees unchanged.', {}),
    'better_rupees': EconomyPreset(
        'Better Rupees',
        'Upgrades rupees to the next tier.',
        {'Rupee (1)': 'Rupees (5)', 'Rupees (5)': 'Rupees (20)', 'Rupees (20)': 'Rupees (50)'}),
    'more_ammo': EconomyPreset(
        'More Ammo',
        'Replace green rupees with nuts, bombs, sticks, bombchus, arrows, and seeds.',
        {'Rupee (1)': ("Deku Nuts (5)", "Bombs (5)", "Deku Stick (1)", "Bombchus (5)", "Arrows (5)", "Deku Seeds (30)")}),
    'gold_rush': EconomyPreset(
        'Gold Rush', 'Gold Rupees everywhere!',
        {'Rupee*': 'Rupees (200)'}),
    'master_swords': EconomyPreset(
        'Master Swords',
        'Replace all rupees with Master Swords.',
        {'Rupee*': 'Master Sword'}),
    'deku_sticks': EconomyPreset(
        'STICKS!',
        'Replace all rupees with sticks',
        {'Rupee (1)': "Deku Sticks (1)"}),
    'bombs': EconomyPreset(
        'Bombs',
        'Replace all rupees with bombs.',
        {'Rupee*': 'Bombs (5)'})
}
ECONOMY_DESCRIPTION = 'Choose how rupee rewards are changed by the selected preset.'


def economy_preset_tooltip():
    """Tooltip text with the setting summary and every preset description."""
    descriptions = '\n'.join(
        f'• {preset.label}: {preset.description}' for preset in ECONOMY_PRESETS.values())
    return f'Rupee Mania!\n{ECONOMY_DESCRIPTION}\n\n{descriptions}'

_MAP_ITEM_KEYS = ('Drop__DropActor', 'Drop__DropActor_Substitute')
_SHOP_ITEM_PREFIX = 'ScrubBusiness_ShopItemActorName'
_DROP_GLOBAL = 'GameParameter/DropGlobalParam/Default.game__drop__DropGlobalParam.bgyml'


class _DropGlobalByml(Byml):
    def value(self, typ, off):
        # The global parameter contains opaque typed arrays unrelated to these edits.
        if typ == 0x20:
            return (typ, self.u32(off))
        return super().value(typ, off)


def _load_byml(raw, resource):
    try:
        return byml_codec.load(raw)
    except (IndexError, struct.error, ValueError) as error:
        raise ValueError(f'Failed to decode BYML {resource}: {error}') from error


def economy_replacements(preset):
    """Replacement rules selected by the seed's Economy preset."""
    try:
        return ECONOMY_PRESETS[preset].replacements
    except KeyError as error:
        raise ValueError(f'Unknown Economy preset: {preset}') from error


def _canonical_actor(actor):
    if actor.endswith('_GS') and actor[:-3] in WORLD_CANONICAL:
        return WORLD_CANONICAL[actor[:-3]]
    if is_derived(actor):
        return canonical_of(actor)
    return WORLD_CANONICAL.get(actor, actor)


def _actor_form(actor, default='chest'):
    if actor.startswith(SHOP_PREFIX):
        return 'shop'
    if (actor in WORLD_CANONICAL or actor.startswith(('Obj_', WORLD_PREFIX, WATER_PREFIX, DROP_PREFIX,
                                                        WATER_DROP_PREFIX))):
        return 'world'
    return default


def _matching_rule(item, replacements):
    for pattern, targets in replacements.items():
        if fnmatchcase(item, pattern):
            return pattern, targets
    return None, None


def _target_options(targets):
    if isinstance(targets, str):
        return (targets,)
    if isinstance(targets, (list, tuple)):
        if not targets or any(not isinstance(target, str) or not target for target in targets):
            raise ValueError('Replacement target lists must contain at least one non-empty item name.')
        return tuple(targets)
    raise TypeError('Replacement targets must be item names or lists of item names.')


def replacement_item(item, replacements):
    """Return the first target of the first matching rule, without advancing a cycle."""
    _, targets = _matching_rule(item, replacements)
    return _target_options(targets)[0] if targets is not None else None


class ReplacementCycle:
    """Assign each source one target, cycling list-valued rules in encounter order."""

    def __init__(self, replacements):
        self.replacements = replacements
        self.positions = {}
        self.assignments = {}
        self.hash_assignments = {}
        self.logical_assignments = {}

    def next_target(self, item, source, actor_hash=None):
        if source in self.assignments:
            return self.assignments[source]
        pattern, targets = _matching_rule(item, self.replacements)
        if targets is None:
            return None
        options = _target_options(targets)
        logical_source = (('map-hash', str(actor_hash), pattern, item)
                          if source[0] == 'map' and actor_hash is not None else source)
        target = self.logical_assignments.get(logical_source)
        if target is None:
            index = self.positions.get(pattern, 0)
            target = options[index % len(options)]
            self.positions[pattern] = index + 1
            self.logical_assignments[logical_source] = target
        self.assignments[source] = target
        if source[0] == 'map' and actor_hash is not None:
            self.hash_assignments[(source[1], str(actor_hash), item)] = target
        return target


def replacement_actor(actor, folder, replacements, form=None, replacement_cycle=None, source=None, actor_hash=None):
    """Return the matching replacement actor in the same form, or ``actor``."""
    canonical = _canonical_actor(actor)
    item = actor_item(canonical, folder)
    target = replacement_item(item, replacements)
    if target is None:
        return actor
    if replacement_cycle is not None and source is not None:
        target = replacement_cycle.next_target(item, source, actor_hash)
    target_actor = item_actor(target, folder)
    target_form = form or _actor_form(actor)
    result = (replacement_bundle_actor(target_actor, target_form)
              or form_actor(target_actor, target_form, skulltula_drop=actor.endswith('_GS')))
    return result


def patch_map_items(raw, path, replacements, required_derived=None, replacement_cycle=None):
    """Replace item actors in map pickups, object drops, and scrub item fields."""
    if not replacements:
        return raw, 0
    replacement_cycle = replacement_cycle or ReplacementCycle(replacements)
    try:
        root = Byml(raw).root
    except (IndexError, struct.error, ValueError) as error:
        raise ValueError(f'Failed to decode map BYML {path}: {error}') from error
    folder = path.split('/')[1] if len(path.split('/')) > 2 else ''
    edits, count = [], 0

    def replace_field(before, field_path, actor_hash, form=None):
        source = ('map', path, field_path)
        updated = replacement_actor(before, folder, replacements, form=form, replacement_cycle=replacement_cycle,
                                    source=source, actor_hash=actor_hash)
        return updated

    for index, actor in enumerate(root.get('Actors', [])):
        if not isinstance(actor, dict):
            continue
        gyaml = actor.get('Gyaml')
        if isinstance(gyaml, str):
            field_path = ('Actors', index, 'Gyaml')
            updated = replace_field(gyaml, field_path, actor.get('Hash'))
            if updated != gyaml:
                edits.append({'path': field_path, 'before': gyaml, 'after': updated})
                if required_derived is not None and is_derived(updated):
                    required_derived.add(updated)
                count += 1
        dynamic = actor.get('Dynamic')
        if not isinstance(dynamic, dict):
            continue
        for key, before in dynamic.items():
            if not isinstance(before, str):
                continue
            field_path = ('Actors', index, 'Dynamic', key)
            if key in _MAP_ITEM_KEYS:
                form = 'drop' if candidate_category(actor) in REPEATABLE else None
                updated = replace_field(before, field_path, actor.get('Hash'), form=form)
            elif key.startswith(_SHOP_ITEM_PREFIX):
                updated = replace_field(before, field_path, actor.get('Hash'), form='event')
            else:
                continue
            if updated != before:
                edits.append({'path': field_path, 'before': before, 'after': updated})
                if required_derived is not None and is_derived(updated):
                    required_derived.add(updated)
                count += 1
    return (patch_strings(raw, edits) if edits else raw), count


def patch_event_items(raw, replacements, resource_path=None, replacement_cycle=None):
    """Replace item actors in flowchart rewards; timeline-only cutscenes are unchanged."""
    if not replacements:
        return raw, 0
    replacement_cycle = replacement_cycle or ReplacementCycle(replacements)
    from oot_rando.game.extra_sources import GIVE_PARAMS

    flow = parse_flow(raw)
    if flow.flowchart is None:
        if flow.timeline is None:
            raise ValueError(f'Event resource {resource_path!r} has neither a flowchart nor a timeline.')
        return raw, 0
    edits = []
    for event in flow.flowchart.events:
        params = getattr(event.data, 'params', None)
        if params is None:
            continue
        for key in GIVE_PARAMS:
            before = params.data.get(key)
            if not isinstance(before, str):
                continue
            source = ('event', resource_path, event.name, key)
            after = replacement_actor(before, '', replacements, form='event',
                                      replacement_cycle=replacement_cycle, source=source)
            if after != before:
                edits.append((event.name, key, before, after))
    return (patch_event_params(raw, edits) if edits else raw), len(edits)


def _source_actor_names(replacements):
    names = set()
    prefixes = (WORLD_PREFIX, WATER_PREFIX, DROP_PREFIX, WATER_DROP_PREFIX, SHOP_PREFIX)

    def include(actor, world=None):
        names.add(actor)
        names.update(prefix + actor for prefix in prefixes)
        if world:
            names.add(world)
            names.add(world + '_GS')

    for actor, item in ACTOR_ITEMS.items():
        if replacement_item(item, replacements) is None:
            continue
        include(actor, NATIVE_WORLD.get(actor))
    for actor, world in NATIVE_WORLD.items():
        if replacement_item(actor_item(actor, ''), replacements) is not None:
            include(actor, world)
    return names


def apply_resource_replacements(members, raw_maps, extra_raw, dictionaries, read, replacements):
    """Apply configured replacements to all game maps and event-flow resources."""
    if not replacements:
        return {'map_fields': 0, 'event_parameters': 0}
    sources = _source_actor_names(replacements)
    if not sources:
        raise ValueError('No game item actors match the configured replacement source names.')
    needles = tuple(actor.encode('utf-8') for actor in sources)
    counts = {'map_fields': 0, 'event_parameters': 0}
    required_derived = set()
    environment_actors = set()
    replacement_cycle = ReplacementCycle(replacements)
    for path in sorted(members):
        is_map = path.startswith('Banc/') and path.endswith('.bcett.byml.zs')
        is_event = path.startswith('Event/EventFlow/') and path.endswith('.bfevfl.zs')
        if not is_map and not is_event:
            continue
        current = raw_maps.get(path) if is_map else extra_raw.get(path)
        if current is None:
            current = decompress(read(path), dictionaries)
        if is_map:
            try:
                actors = Byml(current).root.get('Actors', [])
            except (IndexError, struct.error, ValueError) as error:
                raise ValueError(f'Failed to decode map BYML {path}: {error}') from error
            for actor in actors:
                if (isinstance(actor, dict) and candidate_category(actor) in REPEATABLE
                        and isinstance(actor.get('Gyaml'), str)):
                    environment_actors.add(actor['Gyaml'])
        if not any(actor in current for actor in needles):
            continue
        if is_map:
            changed, count = patch_map_items(current, path, replacements, required_derived,
                                             replacement_cycle)
            if count:
                raw_maps[path] = changed
                counts['map_fields'] += count
        else:
            changed, count = patch_event_items(current, replacements, path, replacement_cycle)
            if count:
                extra_raw[path] = changed
                counts['event_parameters'] += count
    counts['derived_actors'] = sorted(required_derived)
    counts['assignments'] = sorted(replacement_cycle.assignments.items(), key=lambda entry: repr(entry[0]))
    counts['environment_actors'] = sorted(environment_actors)
    counts['hash_assignments'] = sorted(
        ((resource, actor_hash, item, target)
         for (resource, actor_hash, item), target in replacement_cycle.hash_assignments.items()),
        key=repr)
    return counts


def replace_planned_shop_rewards(plan, replacements):
    """Retarget shop and repeatable rewards before their derived packs are generated."""
    if not replacements:
        return 0
    changed = 0
    original_items = {}
    assignments = {}
    replacement_cycle = ReplacementCycle(replacements)
    spoiler = {row['id']: row for row in plan['spoiler']}
    for placement in plan['placements']:
        if placement.get('form') != 'shop' and placement.get('source') not in REPEATABLE:
            continue
        item = placement['item']
        if replacement_item(item, replacements) is None:
            continue
        target = replacement_cycle.next_target(item, ('shop', placement['id']))
        canonical = item_actor(target, placement['folder'])
        form = placement.get('form', 'world')
        actor = replacement_bundle_actor(canonical, form) or form_actor(canonical, form)
        original_items[placement['id']] = item
        assignments[placement['id']] = target
        placement['item'] = target
        placement['after'] = actor
        row = spoiler.get(placement['id'])
        if row is not None:
            row['item'] = row['ap_item'] = target
            row['actor'] = actor
        changed += 1
    plan['item_replacements_original_items'] = original_items
    plan['item_replacements_assignments'] = assignments
    return changed


def prepare_shop_rewards(plan, economy_preset=DEFAULT_ECONOMY_PRESET):
    """Apply configured replacements before generated shop and repeatable actors."""
    return replace_planned_shop_rewards(plan, economy_replacements(economy_preset))


def _build_derived_actors(actors, members, extra_raw, new_files, dictionaries, read, report):
    from oot_rando.game.item_forms import add_rsdb_rows, build_pack, is_derived
    from oot_rando.patching.soul_adapter import RSDB_FILES

    actors = sorted({actor for actor in actors if is_derived(actor)
                     and 'Pack/Actor/' + actor + '.pack.zs' not in members
                     and 'Pack/Actor/' + actor + '.pack.zs' not in new_files})
    if not actors:
        return []
    report(f'Building {len(actors)} replacement actors…')

    def read_pack(actor):
        path = 'Pack/Actor/' + actor + '.pack.zs'
        if path in new_files:
            return sarc(new_files[path][0])
        if path in extra_raw:
            return sarc(extra_raw[path])
        return sarc(decompress(read(path), dictionaries)) if path in members else None

    for actor in actors:
        try:
            new_files['Pack/Actor/' + actor + '.pack.zs'] = build_pack(actor, read_pack, byml_codec)
        except (IndexError, struct.error, ValueError) as error:
            raise ValueError(f'Failed to build derived actor {actor}: {error}') from error
    documents = {name: _load_byml(extra_raw[path] if path in extra_raw
                                  else decompress(read(path), dictionaries), path)
                 for name, path in RSDB_FILES.items()}
    add_rsdb_rows({name: document['root'] for name, document in documents.items()}, actors)
    for name, path in RSDB_FILES.items():
        extra_raw[path] = byml_codec.dump(documents[name])
    return actors


def _patch_drop_pack(raw, pack_path, replacements, replacement_cycle):
    from oot_rando.patching.repeatable_adapter import ACTOR_DROP_TYPE, RUPEE_DROP_TYPE

    files = sarc(raw)
    changed = 0
    required_derived = set()

    def walk(value, member, path=()):
        nonlocal changed
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, member, path + (index,))
            return
        if not isinstance(value, dict):
            return
        actor_ref = value.get('DropActorName')
        prefix, suffix = 'Work/Actor/', '.engine__actor__ActorParam.gyml'
        if isinstance(actor_ref, str) and actor_ref.startswith(prefix) and actor_ref.endswith(suffix):
            actor = actor_ref[len(prefix):-len(suffix)]
            target = replacement_actor(
                actor, '', replacements, form='drop',
                replacement_cycle=replacement_cycle, source=('drop-pack', pack_path, member, path))
            if target != actor:
                value['DropActorName'] = prefix + target + suffix
                value['Type'] = byml_codec.U32(
                    RUPEE_DROP_TYPE if target.startswith('Obj_Rupee_') else ACTOR_DROP_TYPE)
                if is_derived(target):
                    required_derived.add(target)
                changed += 1
        for key, child in value.items():
            walk(child, member, path + (key,))

    for member, member_raw in files.items():
        if (not member.endswith('.bgyml') or member_raw[:2] not in (b'YB', b'BY')
                or b'DropActorName' not in member_raw):
            continue
        document = _load_byml(member_raw, f'{pack_path}:{member}')
        walk(document['root'], member)
        patched = byml_codec.dump(document)
        if patched != member_raw:
            files[member] = patched
    return byml_codec.build_sarc(files), changed, required_derived


def _patch_environment_drops(members, actors, extra_raw, new_files, dictionaries, read, replacements,
                             replacement_cycle):
    changed = packs = 0
    required_derived = set()

    paths = {'Pack/Actor/' + actor + '.pack.zs' for actor in actors}
    paths.update(path for path in new_files if path.startswith('Pack/Actor/') and path.endswith('.pack.zs'))
    for path in sorted(paths):
        if path in new_files:
            raw, template = new_files[path]
        elif path in extra_raw:
            raw, template = extra_raw[path], None
        elif path in members:
            raw, template = decompress(read(path), dictionaries), None
        else:
            continue
        patched, count, derived = _patch_drop_pack(raw, path, replacements, replacement_cycle)
        if count:
            if path in new_files:
                new_files[path] = (patched, template)
            else:
                extra_raw[path] = patched
            changed += count
            packs += 1
            required_derived.update(derived)
    return {'drop_table_items': changed, 'drop_packs': packs}, required_derived


def _patch_global_drop_substitutions(raw, replacements):
    """Replace fallback actors without changing the resource lottery or its conditions."""
    root = _DropGlobalByml(raw).root
    settings = root['DropLotteryMasterData']['ReplaceSetting']
    cycle = ReplacementCycle(replacements)
    edits, derived = [], set()
    prefix, suffix = 'Work/Actor/', '.engine__actor__ActorParam.gyml'
    for i, setting in enumerate(settings):
        for j, entry in enumerate(setting['ReplaceActor']):
            before = entry['ReplaceActor']
            if not isinstance(before, str) or not before.startswith(prefix) or not before.endswith(suffix):
                raise ValueError('Unexpected global drop replacement actor reference.')
            actor = before[len(prefix):-len(suffix)]
            source = ('drop-global', i, j)
            target = replacement_actor(actor, '', replacements, form='drop',
                                       replacement_cycle=cycle, source=source)
            if target != actor:
                edits.append({'path': ('DropLotteryMasterData', 'ReplaceSetting', i, 'ReplaceActor', j,
                                       'ReplaceActor'),
                              'before': before, 'after': prefix + target + suffix})
                if is_derived(target):
                    derived.add(target)
    return (patch_strings(raw, edits, byml_type=_DropGlobalByml) if edits else raw), len(edits), derived


def apply_to_generation(nup, members, raw_maps, extra_raw, plain_raw, new_files, dictionaries, read, report,
                        economy_preset=DEFAULT_ECONOMY_PRESET):
    """Apply configured replacements and build required actor resources."""
    replacements = economy_replacements(economy_preset)
    if not replacements:
        return None
    report('Applying manual item replacements…')
    stats = apply_resource_replacements(
        members, raw_maps, extra_raw, dictionaries, lambda path: nup.read(members[path]), replacements)
    environment_actors = stats.pop('environment_actors')
    drop_stats, drop_derived = _patch_environment_drops(
        members, environment_actors, extra_raw, new_files, dictionaries, read, replacements,
        ReplacementCycle(replacements))
    stats.update(drop_stats)
    global_raw = plain_raw[_DROP_GLOBAL] if _DROP_GLOBAL in plain_raw else read(_DROP_GLOBAL)
    global_raw, count, global_derived = _patch_global_drop_substitutions(global_raw, replacements)
    if count:
        plain_raw[_DROP_GLOBAL] = global_raw
    stats['drop_substitutions'] = count
    actors = stats.pop('derived_actors') + sorted(drop_derived | global_derived)
    stats['derived_actors'] = _build_derived_actors(
        actors, members, extra_raw, new_files, dictionaries, read, report)
    return {'configured': dict(replacements), **stats}


def _variant_source(variant):
    if 'event' in variant:
        return ('event', variant['resource'], variant['event'], variant['param'])
    if 'index' not in variant:
        return None
    field = variant.get('field', 'Drop__DropActor')
    path = (('Actors', variant['index'], 'Gyaml') if field == 'Gyaml'
            else ('Actors', variant['index'], 'Dynamic', field))
    return ('map', variant['resource'], path)


def _replacement_assignments(replacement_stats):
    return {tuple(source): target for source, target in replacement_stats.get('assignments', ())}


def _hash_replacement_assignments(replacement_stats):
    return {(resource, str(actor_hash), item): target
            for resource, actor_hash, item, target in replacement_stats.get('hash_assignments', ())}


def _assigned_target(gid, item, full, world, chests, replacement_stats, replacements):
    assignments = _replacement_assignments(replacement_stats)
    if full:
        shop_assignments = full.get('item_replacements_assignments', {})
        target = shop_assignments.get(gid)
        if target is None:
            target = shop_assignments.get(str(gid))
        if target is not None:
            return target
        entry = full.get('matches', {}).get(gid, {})
        for variant in entry.get('variants', []):
            source = _variant_source(variant)
            if source in assignments:
                return assignments[source]

    hash_assignments = _hash_replacement_assignments(replacement_stats)
    for placement in world.get('placements', []):
        if placement['id'] == gid:
            for resource in placement.get('resources', []):
                target = hash_assignments.get((resource, str(placement.get('hash')), item))
                if target is not None:
                    return target
    from oot_rando.patching.randomize_kokiri import MAP as KOKIRI_MAP
    for chest in chests:
        if str(chest['hash']) == str(gid):
            for resource in (KOKIRI_MAP, KOKIRI_MAP.replace('_Child_', '_Adult_')):
                target = hash_assignments.get((resource, str(chest['hash']), item))
                if target is not None:
                    return target
    return replacement_item(item, replacements)


def update_spoiler(spoiler, full, world, chests, replacement_stats, replacements=None):
    """Update spoiler placements and playthrough labels to match replaced rewards."""
    if replacement_stats is None:
        return 0
    replacements = replacement_stats.get('configured', {}) if replacements is None else replacements
    if not replacements:
        return 0

    rows = {str(row.get('id', row.get('hash', ''))): row
            for row in spoiler.get('placements', [])}
    updated = 0
    playthrough_items = {}
    handled_ids = set()

    def replace_row(gid, item, folder, actor, item_key='item', form=None):
        nonlocal updated
        gid = str(gid)
        target = _assigned_target(gid, item, full, world, chests, replacement_stats, replacements)
        row = rows.get(gid)
        if target is None or row is None:
            return
        changed = False
        if row.get(item_key) != target:
            row[item_key] = target
            changed = True
        if 'ap_item' in row:
            if row['ap_item'] != target:
                row['ap_item'] = target
                changed = True
        target_actor = replacement_actor(actor, folder, {item: target}, form=form)
        if 'after' in row:
            if row['after'] != target_actor:
                row['after'] = target_actor
                changed = True
        if 'actor' in row:
            if row['actor'] != target_actor:
                row['actor'] = target_actor
                changed = True
        if changed:
            if row.get('location'):
                playthrough_items[row['location']] = target
            updated += 1
        handled_ids.add(gid)

    if full:
        original_items = full.get('item_replacements_original_items', {})
        for placement in full.get('placements', []):
            gid = str(placement['id'])
            item = original_items.get(gid, original_items.get(placement['id'], placement['item']))
            actor = rows.get(gid, {}).get('actor', placement.get('after', ''))
            if gid in original_items or placement['id'] in original_items:
                # Shop rewards were already retargeted before their derived actors were built.
                target = _assigned_target(gid, item, full, world, chests, replacement_stats, replacements)
                row = rows.get(gid)
                if target is not None and row is not None:
                    changed = False
                    for key in ('item', 'ap_item'):
                        if key in row and row[key] != target:
                            row[key] = target
                            changed = True
                    if row.get('actor') != placement['after']:
                        row['actor'] = placement['after']
                        changed = True
                    if changed:
                        if row.get('location'):
                            playthrough_items[row['location']] = target
                        updated += 1
                    handled_ids.add(gid)
            else:
                replace_row(gid, item, placement.get('folder', ''), actor)

    for chest in chests:
        gid = str(chest['hash'])
        if gid in handled_ids:
            continue
        item = actor_item(_canonical_actor(chest['after']), 'KokiriForest')
        replace_row(gid, item, 'KokiriForest', chest['after'])
    for placement in world.get('placements', []):
        gid = str(placement['id'])
        if gid in handled_ids:
            continue
        folder = placement['id'].split('#', 1)[0]
        item = actor_item(_canonical_actor(placement['after']), folder)
        form = 'drop' if placement.get('category') in REPEATABLE else None
        replace_row(gid, item, folder, placement['after'], form=form)

    if playthrough_items:
        for sphere in spoiler.get('playthrough', []):
            updated_items = []
            for entry in sphere.get('items', []):
                location, separator, item = entry.rpartition(' : ')
                if separator and location in playthrough_items:
                    entry = location + ' : ' + playthrough_items[location]
                updated_items.append(entry)
            sphere['items'] = updated_items
    return updated


def update_tracker(tracking, full, world, chests, replacement_stats, replacements=None):
    """Update tracker reward labels to match the generated item replacements."""
    if replacement_stats is None:
        return 0
    replacements = replacement_stats.get('configured', {}) if replacements is None else replacements
    if not replacements:
        return 0
    from oot_rando.game.tracking_metadata import reward_label

    rows = tracking.get('checks', {})
    updated = 0

    def replace_row(gid, item):
        nonlocal updated
        target = _assigned_target(gid, item, full, world, chests, replacement_stats, replacements)
        row = rows.get(gid)
        if target is None or row is None:
            return
        reward = reward_label(target)
        changed = row.get('item') != target or row.get('reward') != reward
        if changed:
            row['item'] = target
            row['reward'] = reward
            updated += 1

    if full:
        original_items = full.get('item_replacements_original_items', {})
        placements = {placement['id']: placement for placement in full.get('placements', [])}
        for gid, placement in placements.items():
            replace_row(gid, original_items.get(gid, placement['item']))
        for gid, entry in full.get('matches', {}).items():
            if gid not in placements:
                replace_row(gid, entry.get('vanilla', ''))
    else:
        for chest in chests:
            replace_row(str(chest['hash']), actor_item(_canonical_actor(chest['after']), 'KokiriForest'))
        for placement in world.get('placements', []):
            folder = placement['id'].split('#', 1)[0]
            replace_row(placement['id'], actor_item(_canonical_actor(placement['after']), folder))
    return updated
