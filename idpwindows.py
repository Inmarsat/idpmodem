"""Provides a Windows GUI manager for PC-based IDP modem testing/simulation."""
__version__ = "1.0.1"

import sys

try:
    import Tkinter as tk
except ImportError:
    raise ImportError("Unable to import Tkinter or tkFileDialog.")
import tkFileDialog

try:
    import serialportfinder
except ImportError:
    raise ImportError("Unable to import serialportfinder.py - check root directory.")

# Globals used to enable modification from within a Tkinter custom dialog
_ser_name = "COM0"
_tracking_interval = 60
_debug = False


def initialize(logfile_name=None):
    """
    | Initializes for Windows testing by presenting a dialog to assign COM port and log file name.
    | Also allows user to enable/disable verbose debug and set a tracking interval

    :param logfile_name: the name that will be used if nothing is selected
    :return: ``dictionary`` containing
       - ``serial_name`` e.g. 'COM1'
       - ``logfile`` e.g. 'myLogFile.log'
       - ``debug`` Boolean debug output
       - ``tracking_interval`` (integer) seconds to send location data

    """
    global _ser_name
    global _tracking_interval
    global _debug

    serial_port_list = serialportfinder.listports()
    if len(serial_port_list) == 0 or serial_port_list[0] == '':
        raise ImportError("No serial COM ports found.")

    dialog = tk.Tk()
    dialog.title("Select Options...")
    dialog.geometry("325x150+30+30")
    port_sel_label = tk.Label(dialog, text="Select COM port")
    port_selection = tk.StringVar(dialog)
    port_selection.set(serial_port_list[0])
    option = apply(tk.OptionMenu, (dialog, port_selection) + tuple(serial_port_list))
    option.grid(row=0, column=0, sticky='EW')
    port_sel_label.grid(row=0, column=1, sticky='W')

    dbg_flag = tk.IntVar()
    dbg_checkbox = tk.Checkbutton(dialog, text="Enable debug", variable=dbg_flag)
    dbg_checkbox.grid(row=1, column=0, columnspan=2, padx=5, pady=5)
    dbg_checkbox.select()

    track = tk.IntVar()
    track.set(_tracking_interval)
    track_label = tk.Label(dialog, text="Tracking interval minutes (0..1440)")
    track_label.grid(row=2, column=1, sticky="W")
    track_box = tk.Entry(dialog, text="Tracking interval", textvariable=track, justify='right')
    track_box.grid(row=2, column=0, padx=5, pady=5, sticky="E")

    def ok_select():
        global _ser_name
        global _debug
        global _tracking_interval
        _ser_name = port_selection.get()
        _debug = dbg_flag.get() == 1
        if 0 <= track.get() <= 1440:
            _tracking_interval = track.get() * 60
        dialog.quit()

    def on_closing():
        sys.exit('COM port port_selection cancelled.')

    button_ok = tk.Button(dialog, text='OK', command=ok_select, width=10)
    button_ok.grid(row=3, column=0, padx=5, pady=5)

    button_cancel = tk.Button(dialog, text="Cancel", command=on_closing, width=10)
    button_cancel.grid(row=3, column=1, padx=5, pady=5)

    dialog.protocol('WM_DELETE_WINDOW', on_closing)
    dialog.mainloop()
    dialog.destroy()

    filename = ''
    if logfile_name is not None:
        file_formats = [('Log', '*.log'), ('Text', '*.txt')]
        while filename == '':
            logfile_selector = tk.Tk()
            logfile_selector.withdraw()
            filename = tkFileDialog.asksaveasfilename(defaultextension='.log', initialfile=logfile_name,
                                                      parent=logfile_selector, filetypes=file_formats,
                                                      title="Save log file as...")
            logfile_selector.destroy()

    if _debug:
        print("Serial: %s | Logfile: %s | Tracking: %d seconds | Debug: %s "
              % (_ser_name, filename if logfile_name is not None else "",
                 _tracking_interval, "enabled" if _debug else "disabled"))

    return {
        'serial': _ser_name,
        'logfile': filename,
        'debug': _debug,
        'tracking': _tracking_interval
    }
