#!/usr/bin/env python

import sys
import re
from time import sleep
from tempfile import SpooledTemporaryFile as sptf
from argparse import ArgumentParser
from glob import glob
from os.path import join
from termcolor import colored, cprint
import serial

#### Machine configuration ####
# default units for moves are inches
units = 'mm'

# default mode for moves is absolute
mode = 'abs'

# Protomat C30s steps per mm
# calfactor = 3200 # (old-per inch)
calfactor = 133.33

# hard clip step limits
xmax = 42000 # Proton S42 - PA42000,0 is max
ymax = 24000 # Proton S42 - PA42000,24000 is max

# board start offset in inches
xoff = 0.1
yoff = 0.1

# milling feed in um/s
mill_feed = 12000

# spindle speed in krpm [0..32]
spindle_speed = 32

# time to wait, in seconds, for a drill to complete
drill_dwell = 0.7

# number of HPGL commands to buffer before waiting
serial_queue = 1

#### End machine details ####

#### Start parse functions ####

# HPGL SECRETS
#
# !OC; !RMxxx; !CC -> !RM starts spindle, !OC seems to start dangerous commands until !CC is written.

def change_units(new):
    '''Change units on machine parameters'''
    if new == 'in':
        raise Exception('inch unit is unsupported')


def change_mode(new):
    '''Change mode on machine parameters'''
    global mode
    hpgl = ''
    if mode == new:
        return hpgl
    if mode == 'rel':
        hpgl = 'OS;' +\
               '\nCO "wait for position response"\n'
    print('Changed mode from %s to %s' % (mode, new))
    mode = new
    return hpgl


def parse_move(gcode):
    '''Parse a move command'''
    # TODO blindly initialising this to 0 is bad news bears.
    #       probably ought to query the machine for position, or ensure 'IN;'
    #       is run at the start of each file
    global mode, calfactor, xoff, yoff
    if not hasattr(parse_move, "x"):
        parse_move.x = 0
    if not hasattr(parse_move, "y"):
        parse_move.y = 0
    xycmd = re.match(r"G0[01] X(\S*) Y(\S*).*", gcode)
    if mode == 'abs':
        newx = int(calfactor * (xoff + float(xycmd.group(1))))
        newy = int(calfactor * (yoff + float(xycmd.group(2))))
        if newx > xmax:
            cprint('X move bigger than bed! (%d > %d) when parsing gcode=%s\n' % (newx, xmax, gcode),
                   'red', file=sys.stderr)
            sys.exit(12)
        if newy > ymax:
            cprint('Y move bigger than bed! (%d > %d)\n' % (newy, ymax),
                   'red', file=sys.stderr)
            sys.exit(12)
        hpgl = 'PA%d,%d;' % (newx, newy)
    elif mode == 'rel':
        newx = int(calfactor * (float(xycmd.group(1))))
        newy = int(calfactor * (float(xycmd.group(2))))
        if parse_move.x + newx > xmax:
            cprint('X move bigger than bed! (%d > %d)\n'
                   % (parse_move.x + newx, xmax),
                   'red', file=sys.stderr)
            sys.exit(12)
        if parse_move.y + newy > ymax:
            cprint('Y move bigger than bed! (%d > %d)\n'
                   % (parse_move.y + newy, ymax),
                   'red', file=sys.stderr)
            sys.exit(12)
        hpgl = 'PR%d,%d;' % (parse_move.x + newx, parse_move.y + newy)
    parse_move.x += newx
    parse_move.y += newy
    return hpgl


def parse_z(gcode, drill):
    '''Parse a Z command'''
    global drill_dwell
    zcmd = re.match(r"G0[01] Z(\S*).*", gcode)
    newz = float(zcmd.group(1))
    if newz <= 0.0:
        hpgl = 'PD;'
        if drill:
            hpgl += parse_dwell('G04 P%s' % drill_dwell)
    if newz > 0.0:
        hpgl = 'PU;'
    return hpgl


def parse_dwell(line):
    '''Parse a dwell command'''
    t = float(line[line.find('P') + 1:]) * 1000
    return '!TW%.0f;' % t


def parse_tool_change(gcode):
    '''Parse a tool-change command'''
    global drill_dwell
    newtool = re.match(r"M06 T([0-9]*) \((.*)\).*", gcode)
    tool = newtool.group(1).strip()
    size = newtool.group(2).strip()
    hpgl = 'PA0,0;\nCO "Insert tool #%s: size %s"\n' % (tool, size)
    if size not in ('routing', 'milling'):
        drill_dwell = 20 * float(size)
    return hpgl


def parse_spindle(start):
    '''Start or stop the spindle'''
    global spindle_speed
    if start:
        return '!OC;!RM%d;!CC;' % spindle_speed
        # return '!OC;!RM%d;!CC;!EM1;' % spindle_speed
    else:
        return '!RM0;'
        # return '!OC;!RM0;!CC;!EM0;'

def parse_line(line, drill):
    '''Parse a line of GCODE'''
    hpgl = ''
    if line.startswith('G20'):
        # inch units
        change_units('in')
    elif line.startswith('G21'):
        # mm units
        change_units('mm')
    elif line.startswith('G90'):
        # absolute moves
        hpgl = change_mode('abs')
    elif line.startswith('G91'):
        # relative moves
        hpgl = change_mode('rel')
    elif line.startswith('G00') or line.startswith('G01'):
        # move of some sort
        if 'Z' in line:
            # Z move
            hpgl = parse_z(line, drill)
        else:
            # XY move
            hpgl = parse_move(line)
    elif line.startswith('G04'):
        # dwell
        hpgl = parse_dwell(line)
    elif line.startswith('M03'):
        # start spindle
        hpgl = parse_spindle(start=True)
    elif line.startswith('M05'):
        # stop spindle
        hpgl = parse_spindle(start=False)
    elif line.startswith('M06'):
        # tool change
        hpgl = parse_tool_change(line)
    return hpgl

#### End parse functions ####

#### Start control functions ####


def tool_change(hpgl):
    '''Change tools'''
    drill = hpgl[hpgl.find('"') + 1:hpgl.rfind('"')]
    input(colored('%s\nPress enter when done.' % drill, 'cyan'))


def send_cmd(ser, hpgl, wait=0.1):
    '''Send a command and wait for the response from the machine'''
    ser.write(bytes(hpgl + ";", 'ascii'))
    sleep(wait)
    c = ser.read(128).decode('ascii')
    if 'E' in c:
        cprint('%r\t%r' % (hpgl, c), 'red', file=sys.stderr)
    else:
        cprint('%r\t%r' % (hpgl, c), 'white', file=sys.stdout)

#### End control functions ####


def main():
    '''Main program routine'''
    #### Set up command-line arguments and usage instructions ####
    parser = ArgumentParser(description='Converts EAGLE GCODE (from '
                            'http://pcbgcode.com) into LPKF HGPL, and runs the'
                            ' machine', usage='%(prog)s [options] DIR')
    parser.add_argument('gcode_dir', metavar='DIR', default='./',
                        help='directory in which the gcode files reside')
    parser.add_argument('-d', '--dry-run', dest='dry', action='store_true',
                        default=False, help='only generates HPGL, no machine'
                        ' control')
    parser.add_argument('-o', '--output', metavar='FILE', dest='save_hpgl',
                        default='$$$$TEMP$$$$', help='save the HPGL output in'
                        ' the given location (defaults to a temp file)')
    parser.add_argument('-f', '--file', dest='file', default='',
                        help='which file prefix to use out of the gcode'
                        ' files in the directory')
    ser_opts = parser.add_argument_group('serial port options')
    ser_opts.add_argument('-p', '--port', dest='port', default='/dev/ttyUSB0',
                          help='serial port (default /dev/ttyUSB0)')
    ser_opts.add_argument('-b', '--baud', dest='baud', default=9600, type=int,
                          help='baudrate (default 9600)')

    args = parser.parse_args()

    #### End CLI arguments ####

    #### Start GCODE parse and HPGL generation ####
    # find the correct GCODE files
    print('%s Start GCODE Processing %s' % ('-' * 28, '-' * 28))
    drills = glob(join(args.gcode_dir, args.file + '*drill.g'))
    routes = glob(join(args.gcode_dir, args.file + '*etch.g'))
    mills = glob(join(args.gcode_dir, args.file + '*mill.g'))

    if len(drills) > 1:
        sys.stderr.write('Multiple drill files selected, too confusing!\n\t')
        sys.stderr.write('\n\t'.join(drills) + '\n')
        sys.exit(10)
    if drills:
        layer = drills[0][-11:-8]
        olayer = ('top', 'bot')[layer == 'top']
    else:
        layer = 'top'
        olayer = 'bot'

    print('Using layer %s as first layer based on drill file.' % layer)
    print('Machine is on %s at %dbaud' % (args.port, args.baud))
    print('Machine will mill at %dum/s and spin up to %drpm.'
          % (mill_feed, spindle_speed * 1000))
    print('Board offset is X=%.6f%s, Y=%.6f%s, max bed is X=%.2f%s, Y=%.2f%s'
          % (xoff, units, yoff, units, xmax / calfactor, units, ymax / calfactor, units))
    print('Board will be milled in %s mode and use %s as units' % (mode, units))
    if not mills:
        print(colored('No milling layer present.', 'yellow'))
    if not drills:
        print(colored('No drilling layer present.', 'yellow'))
    if not routes:
        print(colored('No routing layer present.', 'yellow'))

    # determine if we need a temp file or a real one
    hpgl_file = None
    if args.save_hpgl == '$$$$TEMP$$$$':
        hpgl_file = sptf(max_size=10000000, mode='w')
        print('Producing HPGL output in tempfile')
    else:
        hpgl_file = open(args.save_hpgl, 'w')
        print('Producing HPGL output in %s' % args.save_hpgl)

    hpgl_file.write('IN;!CT1;VS%d;!OC;!SV140;!SM32;!WR0,8,8;!CC;!CM1;'
                    % (mill_feed))
    number = 0
    # drills first
    if drills:
        with open(drills[0]) as f:
            print('%s Drills %s' % ('=' * 36, '=' * 36))
            for line in f:
                line = line.strip()
                if line.startswith('('):
                    # comment
                    continue
                hpgl = parse_line(line, drill=True)
                number += 1
                print()
                '%d\t%s\t\t%s%s' % (number, line, ('', '\t')[len(line) < 16],
                                    hpgl.strip())
                hpgl_file.write(hpgl)
            print('%s End Drills %s' % ('=' * 34, '=' * 34))

    # routing on the drill layer next
    if routes:
        with open([f for f in routes if layer in f][0]) as f:
            print('%s %s Traces %s' % ('=' * 34, layer, '=' * 34))
            hpgl_file.write(parse_tool_change('M06 T98 (routing )'))
            for line in f:
                line = line.strip()
                if line.startswith('('):
                    # comment
                    continue
                hpgl = parse_line(line, drill=False)
                number += 1
                print('%d\t%s\t\t%s%s' % (number, line, ('', '\t')[len(line) < 16],
                                          hpgl.strip()))
                hpgl_file.write(hpgl)
            print('%s End %s Traces %s' % ('=' * 32, layer, '=' * 32))

    hpgl_file.write(parse_spindle(start=False))
    hpgl_file.write('PU;')
    # hpgl_file.write('PU;PA%d,%d;' % (xmax, 0))
    hpgl_file.write('\nCO "Please flip board."\n')

    # routing on other layer
    if len(routes) > 1:
        with open([f for f in routes if olayer in f][0]) as f:
            print('%s %s Traces %s' % ('=' * 34, olayer, '=' * 34))
            for line in f:
                line = line.strip()
                if line.startswith('('):
                    # comment
                    continue
                hpgl = parse_line(line, drill=False)
                number += 1
                print('%d\t%s\t\t%s%s' % (number, line, ('', '\t')[len(line) < 16],
                                          hpgl.strip()))
                hpgl_file.write(hpgl)
            print('%s End %s Traces %s' % ('=' * 32, olayer, '=' * 32))

    # milling layer last
    mills = [f for f in mills if olayer in f]
    if mills:
        with open(mills[0]) as f:
            hpgl_file.write(parse_tool_change('M06 T99 (milling )'))
            print('%s %s Traces %s' % ('=' * 34, layer, '=' * 34))
            for line in f:
                line = line.strip()
                if line.startswith('('):
                    # comment
                    continue
                hpgl = parse_line(line, drill=False)
                number += 1
                print('%d\t%s\t\t%s%s' % (number, line, ('', '\t')[len(line) < 16],
                                          hpgl.strip()))
                hpgl_file.write(hpgl)
            print('%s End %s Traces %s' % ('=' * 32, layer, '=' * 32))

    hpgl_file.write('!OC;!RM0;!CC;PU;')
    # hpgl_file.write('!OC;!RM0;!CC;PU;!EM0;PA%d,%d;' % (xmax, 0))

    print('%s End GCODE Processing %s' % ('-' * 29, '-' * 29))
    #### End HPGL generation ####

    #### Serial output (machine control) ####
    print('%s Start RS-232 Control %s' % ('-' * 29, '-' * 29))
    if args.dry:
        print(colored('Machine control disabled, dry run!', 'yellow'))

        class SerialDummy():
            def read(self, count):
                return ''

            def inWaiting(self):
                return 0

            def write(self, data):
                pass

        ser = SerialDummy()
    else:
        ser = serial.Serial(args.port, args.baud, timeout=0.01)

    # discard anything in the input buffer before starting
    ser.read(ser.inWaiting())

    hpgl_file.seek(0, 0)
    for line in hpgl_file.read().split('\n'):
        print(colored(repr(line.strip()), 'green'))
        for command in line.split(";"):
            if command:
                if command.startswith('CO'):
                    if 'tool' in command:
                        tool_change(command)
                    elif 'flip' in command:
                        input(colored('Please flip board.\nPress Enter'
                                      ' when ready.', 'cyan'))
                else:
                    send_cmd(ser, command)
    input(colored('Wait until plotter finishes and press enter to'
                      ' exit', 'cyan'))
    print('%s End RS-232 Control %s' % ('-' * 30, '-' * 30))
    #### End Machine control ####


if __name__ == '__main__':
    main()
