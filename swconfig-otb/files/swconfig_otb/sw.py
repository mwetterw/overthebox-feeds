# -*- coding: utf-8 -*-
# vim: set expandtab tabstop=4 shiftwidth=4 softtabstop=4 :
"""OTBv2 Switch module

This module implements primitives to serially interact with the TG-NET S3500-15G-2F switch.
"""

import re
import logging
import serial

from swconfig_otb.sw_state import _States
from swconfig_otb.exception import SwitchBadEchoBudgetExceededError
import swconfig_otb.config as config

logger = logging.getLogger('swconfig')


class Sw(object):
    """Represent a serial connection to a TG-NET S3500-15G-2F switch."""

    _MORE_MAGIC = ["\x08", "\x1b[A\x1b[2K"]

    # This is a trick used to be able to define some parts of the class in a separated file
    from swconfig_otb.sw_vlan import _set_diff, _dict_diff, _str_to_if_range
    from swconfig_otb.sw_vlan import _parse_vlans, _create_vid, _delete_vid
    from swconfig_otb.sw_vlan import update_vlan_conf, init_vlan_config_datastruct

    def __init__(self):
        self.sock = serial.Serial()

        self.sock.port = config.PORT
        self.sock.baudrate = config.BAUDRATE
        self.sock.bytesize = config.BYTESIZE
        self.sock.parity = config.PARITY
        self.sock.stopbits = config.STOPBITS
        self.sock.timeout = config.READ_TIMEOUT
        self.sock.write_timeout = config.WRITE_TIMEOUT
        self.sock.inter_byte_timeout = config.INTER_BYTE_TIMEOUT
        self.state = None
        self.hostname = None
        self._bad_echo = 0

    def __enter__(self):
        self.open_()
        return self

    def __exit__(self, type_, value, traceback):
        self.close()

    def open_(self):
        """Open the serial connection and go to the admin main prompt

        Instead of calling me, consider using 'with' statement if that suits your needs
        """
        self.sock.open()
        self._goto_admin_main_prompt()

    def close(self):
        """Close the serial connection and reset the state so this instance could be reused

        Instead of calling me, consider using 'with' statement if that suits your needs
        """
        self.sock.close()
        self.state = None
        self.hostname = None
        self._bad_echo = 0

    def _recv(self, auto_more, timeout):
        """Receive everything. If needed, we'll ask the switch for MOOORE. :p

        Some commands may activate a pager when the answer becomes too big.
        We would then stay stuck with a --More-- at the bottom.
        This method receives output as many times as needed and gather the whole output.

        Args:
            auto_more: When true, we'll keep asking for more and get the full output
                Otherwise the More logic is disabled and --More-- will be received in the output
                It will be up to the caller to deal with the fact that we're still in a More state
            timeout: If the cmd is known to require a longer Switch CPU processing time than usual,
                a timeout can be specified. It will be used only for the first read.
        """
        self.sock.timeout = timeout # Increase the timeout to the one specified
        whole_out, all_comments = self._recv_once_retry()
        self.sock.timeout = config.READ_TIMEOUT # Reset to a lower timeout for subsequent reads

        # If we're now in a more, ask for MOOOORE to get the full output! :p
        while auto_more and self.state == _States.MORE:
            self.sock.write(" ") # Sending a space gives us more lines than a LF
            out, comments = self._recv_once_retry()

            if out[0] == self._MORE_MAGIC[0] and out[1].startswith(self._MORE_MAGIC[1]):
                out.pop(0) # Remove the BackSpace (it occupies a whole line)
                out[0] = out[0][len(self._MORE_MAGIC[1]):] # Strip the ^[A^[2K from the second line

            whole_out.extend(out)
            all_comments.extend(comments)

        return (whole_out, all_comments)

    def _recv_once_retry(self):
        """Try to receive once, and retries with increasing timeout if it fails"""
        for _ in range(config.READ_RETRIES):
            try:
                return self._recv_once()
            except serial.SerialTimeoutException:
                logger.error("Read failed with timeout %fs. Will retry...", self.sock.timeout)
                self.sock.timeout = self.sock.timeout * 2

        raise serial.SerialTimeoutException("All read attempts timed out. Is the switch dead?")

    def _recv_once(self):
        """Receive once, filter output and update switch state by parsing prompt"""
        # First, call self.readlines().
        # It reads from serial port and gets a list with one line per list item.
        # In each of the list's item, remove any \r or \n, but only at end of line (right strip)
        # For each line of the output, give it to _filter
        # This will filter out switch comments and put them into a separated list
        # Finally, use filter(None, list) to remove empty elements
        coms = []
        out = filter(None, [self._filter(l.rstrip("\r\n"), coms) for l in self.sock.readlines()])

        # out should never be empty. Otherwise it means we have a problem...
        if not out:
            raise serial.SerialTimeoutException("The read timed out.")

        # However, out may become empty after prompt parsing (prompt will be removed)
        self.state = self._parse_prompt(out)
        if self.state:
            logger.debug("Switch state is: '%s'", self.state.name)
        else:
            logger.error("Switch state is unknown")

        return (out, coms)

    @staticmethod
    def _filter(line, comments):
        """Remove comments and push them to a separated list

        Comments always end by CRLF, sometimes after prompt, sometimes on a new line
        They start with a '*' and a date in the form '*Jan 13 2000 11:25:20: '
        Then come a type prefix and the message:
            %System-5: New console connection for user admin, source async  ACCEPTED
            %Port-5: Port gi6 link down
            %Port-5: Port gi4 link up

        Args:
            line: The current line being processed
            comments: A reference to a list where we can push the comments we find

        Returns:
            The modified output line (it can become empty if the whole line was a comment)
        """
        # This regex matches a switch comment
        comment_regex = re.search(r'(\*.*: %.*: .*)', line)

        # If we've found a comment, move it to a dedicated list
        if comment_regex and comment_regex.group():
            comments.append(comment_regex.group())
            line = line[:comment_regex.span()[0]]

        return line

    def _send(self, string, bypass_echo_check=True, auto_more=False, timeout=config.READ_TIMEOUT):
        """Send an arbitrary string to the switch and get the answer

        Args:
            string: The string to send to the switch
            bypass_echo_check: When True, the echo will be part of the global answer
                Otherwise it'll be consumed char by char and will be checked
            auto_more: When true, we'll keep asking for more and get the full output
                Otherwise the More logic is disabled and --More-- will be received in the output
                It will be up to the caller to deal with the fact that we're still in a More state
            timeout: If the cmd is known to require a longer Switch CPU processing time than usual,
                a timeout can be specified. It will be used only for the first read.
        """
        # When sending a command, it's safer to send it char by char, and wait for the echo
        # Why? Try to connect to the switch, go to the Username: prompt.
        # Then, in order to simulate high speed TX, copy "admin" and paste it inside the console.
        # The echo arrives in a random order. The behaviour is completely unreliable.
        # (Actually, only the echo arrives out of order. But the switch got it in the right order.)
        for char in string:
            self.sock.write(char)

            # If we don't care about echo, don't consume and don't check it
            if bypass_echo_check:
                continue

            # Skip Carriage Return (we never send CR, the switch always echo with CR)
            echo = self.sock.read(1)
            echo = echo if echo != "\r" else self.sock.read(1)

            # Each character we get should be the echo of what we just sent
            # '*' is also considered to be a good password echo
            # If we encounter wrong echo, maybe we just got a garbage line from the switch
            # In that case we flush the input buffer so that we stop reading the garbage immediately
            # That way, the next time we read one character, it should be again our echo
            # We only tolerate a given fixed "wrong echo budget"
            # Note: In password echo at the end, there is a "\n" echo which is considered correct
            expected = '*' if self.state == _States.LOGIN_PASSWORD and echo != char else char
            if echo != expected:
                self._bad_echo = self._bad_echo + 1
                logger.warn("Invalid echo: expected '%c' (%s), got '%c' (%s)",
                            expected, hex(ord(expected)), echo, hex(ord(echo)))
                self.sock.flushInput()

                if self._bad_echo > config.BAD_ECHO_BUDGET:
                    msg = "Bad echo budget exceeded. Giving up."
                    logger.error(msg)
                    raise SwitchBadEchoBudgetExceededError(msg)

        return self._recv(auto_more, timeout)

    def send_cmd(self, cmd, timeout=config.READ_TIMEOUT):
        """Send a command to the switch, check the echo and get the full output.

        Args:
            cmd: The command to send. Do not add any LF at the end.
            timeout: If the cmd is known to require a longer Switch CPU processing time than usual,
                a timeout can be specified. It will be used only for the first read.

        Returns:
            A tuple (out, comments)
                out: List of strings of the regular output (no comments inside)
                comments: List of strings of the switch comments
        """
        return self._send("%s\n" % (cmd), False, True, timeout)

    def _goto_admin_main_prompt(self):
        """Bring the switch to the known state "hostname# " prompt (from known or unknown state)

        If necessary, it will login, exit some menus, escape an ongoing "--More--"...
        """
        self.sock.flushInput()

        # We don't know where we are, let's find out :)
        if self.state in [None, _States.PRESS_ANY_KEY]:
            # We don't know where we are: we don't know if our keystroke will produce an echo or not
            # So when sending our keystroke, we disable the consumption and check of the echo
            # This allows us to analyze the full answer ourselves and then determine the state
            self._send("\n")

        #Now, we know where we are. Let's go to the ADMIN_MAIN state :)
        res = None

        # We are already where we want to go. Stop here
        if self.state == _States.ADMIN_MAIN:
            return True

        if self.state == _States.MORE:
            logger.debug("Sending one ETX (CTRL+C) to escape --More-- state")
            res, _ = self._send("\x03")

        # We are logged in and at the "hostname> " prompt. Let's enter "hostname# " prompt
        elif self.state == _States.USER_MAIN:
            res, _ = self.send_cmd("enable")

        # We're logged in and in some menus. Just exit them
        elif self.state in [_States.CONFIG, _States.CONFIG_VLAN,
                            _States.CONFIG_IF, _States.CONFIG_IF_RANGE]:
            res, _ = self.send_cmd("end")

        # We're in the login prompt. Just login now!
        elif self.state in [_States.LOGIN_USERNAME, _States.LOGIN_PASSWORD]:
            res = self._login()

        # Only return true if we succeeded to bring the switch to the ADMIN_MAIN state
        return res and self.state == _States.ADMIN_MAIN

    def _parse_prompt(self, out):
        """Analyze the received output to determine the switch state

        Args:
            out: A list with the output that we'll use to determine the state

        Returns:
            A state if we found out, or None if we still don't known where we are
        """
        last_line = out[-1]

        # States without hostname information in the prompt
        for state in [_States.PRESS_ANY_KEY, _States.LOGIN_USERNAME,
                      _States.LOGIN_PASSWORD, _States.MORE]:
            if last_line.startswith(state.prompt_needle):
                if state == _States.MORE:
                    out.pop() # Remove the --More-- from the output!
                return state

        # Hostname determination
        if not self.hostname and not self._determine_hostname(last_line):
            return None

        # States containing the hostname in the prompt
        for state in [_States.CONFIG, _States.CONFIG_VLAN, _States.CONFIG_IF,
                      _States.CONFIG_IF_RANGE, _States.ADMIN_MAIN, _States.USER_MAIN]:
            if last_line.startswith(self.hostname + state.prompt_needle):
                out.pop() # Remove the prompt from the output :)
                return state

        # Unknown state
        return None

    def _determine_hostname(self, output_last_line):
        """Extract the hostname from the prompt and store it"""
        hostname_regex = re.search(r'(?P<hostname>[^(]+).*(?:>|#) ', output_last_line)
        if hostname_regex and hostname_regex.group('hostname'):
            self.hostname = hostname_regex.group('hostname')
            logger.debug("Hostname '%s' detected", self.hostname)
            return True
        else:
            logger.error("Unable to determine hostname :'(")
            return False

    # It can only be called when we are in the LOGIN_USERNAME or LOGIN_PASSWORD state
    def _login(self):
        """Automatically login into the switch

        Only call me if we are in the LOGIN_USERNAME or LOGIN_PASSWORD state.
        """
        if self.state == _States.LOGIN_USERNAME:
            out, _ = self.send_cmd(config.USER)
            # The switch rejects us immediately if the username doesn't exist
            if any("Incorrect User Name" in l for l in out):
                logger.error("The switch claims the username is invalid. " \
                      "Check that the credentials are correct.")
                return False

            # We should have entered password state as soon as correct username has been sent
            if self.state != _States.LOGIN_PASSWORD:
                logger.error("Unexpected error after sending username to the switch: " \
                      "we should have entered password state")
                return False
            # If we got here, the login has been accepted. Let's continue and send the password

        # There are 2 possible execution flows:
        #  1) We've just sent the login above, now it's time to send the password
        #  2) We arrive directly here as the first if above was skipped
        # This 2th case is rare but could occur if the state is following before launching swconfig:
        # (Username fully typed in but no Line Feed entered)
        #  Username: admin
        # In this case we'll transition from UNKNOWN to LOGIN_PASSWORD state directly
        if self.state == _States.LOGIN_PASSWORD:
            out, comments = self.send_cmd(config.PASSWORD)
            if any("ACCEPTED" in c for c in comments):
                return True

            if any("REJECTED" in c for c in comments):
                logger.error("The switch rejected the password. " \
                             "Check that the credentials are correct.")
                return False

            logger.error("Unexpected error after sending password: no ACCEPTED nor REJECTED found")
            return False

        # What? Have I been called in a state that is not LOGIN_USERNAME nor LOGIN_PASSWORD?
        logger.critical("Login method should never have been called now. Bye bye...")
        assert False
