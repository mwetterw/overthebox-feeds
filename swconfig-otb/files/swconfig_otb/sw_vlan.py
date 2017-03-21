# -*- coding: utf-8 -*-
# vim: set expandtab tabstop=4 shiftwidth=4 softtabstop=4 :
# pylint: disable=protected-access
"""OTBv2 Switch module, extension for VLANs

This module adds methods to the class Sw, all related to VLAN management
"""

import logging
from swconfig_otb.config import PORT_MIN, PORT_MAX, DEFAULT_VLAN
from swconfig_otb.sw_state import _States

logger = logging.getLogger('swconfig')


def update_vlan_conf(self, vlans_new, ports_new):
    vlans_old, ports_old = self._parse_vlans()
    logger.debug("Wanted VLANs: %s", vlans_new)
    logger.debug("Wanted interfaces configuration %s", ports_new)

    logger.debug("Current VLANs: %s", vlans_old)
    logger.debug("Current interfaces configuration %s", ports_old)

    vlan_added, vlan_removed = self._set_diff(vlans_old, vlans_new)
    logger.debug("VIDs to be added: %s", vlan_added)
    logger.debug("VIDs to be removed: %s", vlan_removed)

    ports_changed = self._dict_diff(ports_old, ports_new)
    logger.debug("IFs to be updated: %s", ports_changed)

    if not ports_changed and not vlan_added and not vlan_removed:
        logger.info("Switch VLAN conf is the same as wanted conf. Nothing to do. :)")
        return

    # Let's make the switch obeeeyyyyy! :p
    self._goto_admin_main_prompt()

    self.send_cmd('config')
    for vlan in vlan_removed:
        logger.info('Deleting VLAN %d', vlan)
        self.send_cmd('no vlan %d' % (vlan))

    for vlan in vlan_added:
        logger.info('Creating VLAN %d', vlan)
        self.send_cmd('vlan %d' % (vlan))
        self.send_cmd('exit')

    for port_id, vids in ((port_id, ports_new[port_id]) for port_id in ports_changed):
        logger.info('Configuring interface %d', port_id)
        self.send_cmd('interface GigabitEthernet %d' % (port_id))

        # This port is at least tagged on one VID. The port will need to be in trunk mode.
        if vids['tagged']:
            logger.info(' Putting interface into trunk mode')
            self.send_cmd('switchport mode trunk')

            # Determine the native VID and set it
            native_vlan = vids['untagged'] if vids['untagged'] else DEFAULT_VLAN
            logger.info(' Setting interface native VID to %d', native_vlan)
            self.send_cmd('switchport trunk native vlan %d' % (native_vlan))

            ifv_added, ifv_removed = self._set_diff(ports_old[port_id]['tagged'], vids['tagged'])

            # Remove VIDs this interface doesn't belong to anymore
            if ifv_removed:
                ifv_removed_range = ','.join(str(v) for v in ifv_removed)
                logger.info(' Removing obsolete VIDs %s for this interface', ifv_removed_range)
                self.send_cmd('switchport trunk allowed vlan remove %s' % (ifv_removed_range))

            # Make the interface belong to new VIDs that it didn't belong to before
            if ifv_added:
                ifv_added_range = ','.join(str(v) for v in ifv_added)
                logger.info(' Adding new VIDs %s for this interface', ifv_added_range)
                self.send_cmd('switchport trunk allowed vlan add %s' % (ifv_added_range))

        # This port is only untagged, it's never tagged. The port will need to be in access mode.
        else:
            logger.info(' Putting interface into access mode')
            self.send_cmd('switchport mode access')
            logger.info(' Setting interface VID to %d', vids['untagged'])
            self.send_cmd('switchport access vlan %d' % (vids['untagged']))

        self.send_cmd('exit')
    self.send_cmd('end')


def _parse_vlans(self):
    """Ask the switch its VLAN state and return it

    Returns:
        A set and a dictionary of dictionary.
        - The set just contains all the existing VIDs on the switch.
        - For the dict: first dict layer has the interface number as key.
            The second layer has two keys: 'untagged' and 'tagged'.
                Key 'untagged': the value is either None or only one VID value
                Key 'tagged': the value is a set of VID this interface belongs to
    """
    out, _ = self.send_cmd("show vlan static")

    # Initialize our two return values
    vlans, ports = self.init_vlan_config_datastruct()

    # Skip header and the second line (-----+-----...)
    for line in out[2:]:
        row = [r.strip() for r in line.split('|')]
        vid, untagged, tagged = int(row[0]), row[2], row[3]

        vlans.add(vid)

        untagged_range = self._str_to_if_range(untagged)
        tagged_range = self._str_to_if_range(tagged)

        for if_ in untagged_range:
            if ports[if_]['untagged'] is None:
                ports[if_]['untagged'] = vid
            else:
                logger.warning("Skipping subsequent untagged VIDs for port %d. " \
                               "Value was %s", if_, ports[if_]['untagged'])

        for if_ in tagged_range:
            ports[if_]['tagged'].add(vid)

    return vlans, ports

@staticmethod
def init_vlan_config_datastruct():
    """Initialize an empty vlan config data structure"""
    vlans = set()
    ports = {key: {'untagged': None, 'tagged': set()} for key in range(PORT_MIN, PORT_MAX + 1)}

    return vlans, ports

@staticmethod
def _str_to_if_range(string):
    """Take an interface range string and generate the expanded version in a list.

    Only the interface ranges starting with 'gi' will be taken into account.

    Args:
        string: A interface range string (ex 'gi4,gi6,gi8-10,gi16-18,lag2')

    Returns:
        A list of numbers which is the expansion of the whole range. For example,
            the above input will give [4, 6, 8, 9, 10, 16, 17, 18]
    """
    # Split the string by ','.
    # Exclude elements that don't start with "gi" (we could have 'lag8-15', or '---').
    # Then, remove the 'gi' prefix and split by '-'. We end up with a list of lists.
    # This is a list of the ranges bounds (1 element or 2: start and end bounds).
    # Then, return a list of all the concatenated expanded ranges.
    # The trick of using a[0] and a[-1] allows it to work with single numbers as well.
    # This wouldn't be the case if we had used a[0] and a[1].
    # If there's only one digit [1], it will compute range(1, 1 + 1) which is 1.
    range_ = [r[len('gi'):].split('-') for r in string.split(',') if r.startswith('gi')]
    return [i for r in range_ for i in range(int(r[0]), int(r[-1]) + 1)]

@staticmethod
def _set_diff(old, new):
    intersect = new.intersection(old)
    added = new - intersect
    removed = old - intersect

    return added, removed

@staticmethod
def _dict_diff(old, new):
    set_old, set_new = set(old.keys()), set(new.keys())
    intersect = set_new.intersection(set_old)

    changed = set(o for o in intersect if old[o] != new[o])
    return changed