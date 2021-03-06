"""
The loader module manages the workflow of loading a new pyRevit session.
It's main purpose is to orchestrate the process of finding pyRevit extensions,
creating dll assemblies for them, and creating a user interface
in the host application.

Everything starts from ``sessionmgr.load_session()`` function...

The only public function is ``load_session()`` that loads a new session.
Everything else is private.
"""

import os.path as op
import sys

from pyrevit import EXEC_PARAMS, HOST_APP
from pyrevit import coreutils
from pyrevit.coreutils import Timer
from pyrevit.coreutils.appdata import cleanup_appdata_folder
from pyrevit.coreutils.logger import get_logger, get_stdout_hndlr, \
                                     loggers_have_errors
# import the basetypes first to get all the c-sharp code to compile
from pyrevit.loader.asmmaker import create_assembly, cleanup_assembly_files
from pyrevit.output import get_output
from pyrevit.userconfig import user_config
from pyrevit.extensions.extensionmgr import get_installed_ui_extensions
from pyrevit.usagelog import setup_usage_logfile
from pyrevit.versionmgr.upgrade import upgrade_existing_pyrevit
from pyrevit.loader import sessioninfo
from pyrevit.loader.uimaker import update_pyrevit_ui, cleanup_pyrevit_ui
from pyrevit.extensions import COMMAND_AVAILABILITY_NAME_POSTFIX
from pyrevit.loader.basetypes import LOADER_BASE_NAMESPACE
from pyrevit import DB, UI, revit
from pyrevit.framework import FormatterServices
from pyrevit.framework import Array


logger = get_logger(__name__)


def _clear_running_engines():
    # clear the cached engines
    try:
        my_output = get_output()
        if my_output:
            my_output.close_others(all_open_outputs=True)

        EXEC_PARAMS.engine_mgr.ClearEngines()
    except AttributeError:
        return False


def _setup_output():
    from pyrevit.coreutils import loadertypes
    # create output window and assign handle
    out_window = loadertypes.ScriptOutput()

    # create output stream and set stdout to it
    # we're not opening the output window here.
    # The output stream will open the window if anything is being printed.
    outstr = loadertypes.ScriptOutputStream(out_window)
    sys.stdout = outstr
    # sys.stderr = outstr
    stdout_hndlr = get_stdout_hndlr()
    stdout_hndlr.stream = outstr

    return out_window


def _cleanup_output():
    sys.stdout = None
    stdout_hndlr = get_stdout_hndlr()
    stdout_hndlr.stream = None


# -----------------------------------------------------------------------------
# Functions related to creating/loading a new pyRevit session
# -----------------------------------------------------------------------------
def _perform_onsessionload_ops():
    # clear the cached engines
    if not _clear_running_engines():
        logger.debug('No Engine Manager exists...')

    # once pre-load is complete, report environment conditions
    uuid_str = sessioninfo.new_session_uuid()
    sessioninfo.report_env()

    # reset the list of assemblies loaded under pyRevit session
    sessioninfo.set_loaded_pyrevit_assemblies([])

    # asking usagelog module to setup the usage logging system
    # (active or not active)
    setup_usage_logfile(uuid_str)

    # apply Upgrades
    upgrade_existing_pyrevit()


def _perform_onsessionloadcomplete_ops():
    # cleanup old assembly files.
    # asmmaker.cleanup_assembly_files() will take care of that
    cleanup_assembly_files()

    # clean up temp app files between sessions.
    cleanup_appdata_folder()


def _new_session():
    """
    Get all installed extensions (UI extension only) and creates an assembly,
    and a ui for each.

    Returns:
        None
    """

    loaded_assm_list = []
    # get all installed ui extensions
    for ui_ext in get_installed_ui_extensions():
        # create a dll assembly and get assembly info
        ext_asm_info = create_assembly(ui_ext)
        if not ext_asm_info:
            logger.critical('Failed to create assembly for: {}'
                            .format(ui_ext))
            continue

        logger.info('Extension assembly created: {}'.format(ui_ext.name))

        # add name of the created assembly to the session info
        loaded_assm_list.append(ext_asm_info.name)

        # run startup scripts for this ui extension, if any
        if ui_ext.startup_script:
            logger.info('Running startup tasks...')
            logger.debug('Executing startup script for extension: {}'
                         .format(ui_ext.name))
            execute_script(ui_ext.startup_script)

        # update/create ui (needs the assembly to link button actions
        # to commands saved in the dll)

        update_pyrevit_ui(ui_ext,
                          ext_asm_info,
                          user_config.core.get_option('loadbeta',
                                                      default_value=False))
        logger.info('UI created for extension: {}'.format(ui_ext.name))

    # add names of the created assemblies to the session info
    sessioninfo.set_loaded_pyrevit_assemblies(loaded_assm_list)

    # cleanup existing UI. This is primarily for cleanups after reloading
    cleanup_pyrevit_ui()


def load_session():
    """Handles loading/reloading of the pyRevit addin and extensions.
    To create a proper ui, pyRevit extensions needs to be properly parsed and
    a dll assembly needs to be created. This function handles these tasks
    through interactions with .extensions, .loader.asmmaker, and .loader.uimaker

    Example:
        >>> from pyrevit.loader.sessionmgr import load_session
        >>> load_session()     # start loading a new pyRevit session

    Returns:
        None
    """
    # the loader dll addon, does not create an output window
    # if an output window is not provided, create one
    if EXEC_PARAMS.first_load:
        output_window = _setup_output()
    else:
        from pyrevit import script
        output_window = script.get_output()

    # initialize timer to measure load time
    timer = Timer()

    # perform pre-load tasks
    _perform_onsessionload_ops()

    # create a new session
    _new_session()

    # perform post-load tasks
    _perform_onsessionloadcomplete_ops()

    # log load time and thumbs-up :)
    endtime = timer.get_time()
    success_emoji = ':ok_hand_sign:' if endtime < 3.00 else ':thumbs_up:'
    logger.info('Load time: {} seconds {}'.format(endtime, success_emoji))

    # if everything went well, self destruct
    try:
        timeout = user_config.core.startuplogtimeout
        if timeout > 0 and not loggers_have_errors():
            if EXEC_PARAMS.first_load:
                # output_window is of type ScriptOutput
                output_window.SelfDestructTimer(timeout)
            else:
                # output_window is of type PyRevitOutputWindow
                output_window.self_destruct(timeout)
    except Exception as imp_err:
        logger.error('Error setting up self_destruct on output window | {}'
                     .format(imp_err))

    _cleanup_output()


# -----------------------------------------------------------------------------
# Functions related to finding/executing
# pyrevit command or script in current session
# -----------------------------------------------------------------------------
class PyRevitExternalCommandType(object):
    def __init__(self, extcmd_type, extcmd_availtype):
        self._extcmd_type = extcmd_type
        self._extcmd = extcmd_type()
        if extcmd_availtype:
            self._extcmd_availtype = extcmd_availtype
            self._extcmd_avail = extcmd_availtype()
        else:
            self._extcmd_availtype = None
            self._extcmd_avail = None

    @property
    def extcmd_type(self):
        return self._extcmd_type

    @property
    def typename(self):
        return self._extcmd_type.FullName

    @property
    def extcmd_availtype(self):
        return self._extcmd_availtype

    @property
    def avail_typename(self):
        return self._extcmd_availtype.FullName

    @property
    def script(self):
        return getattr(self._extcmd, 'baked_scriptSource', None)

    @property
    def alternate_script(self):
        return getattr(self._extcmd, 'baked_alternateScriptSource', None)

    @property
    def syspaths(self):
        return getattr(self._extcmd, 'baked_syspaths', None)

    @property
    def helpsource(self):
        return getattr(self._extcmd, 'baked_helpSource', None)

    @property
    def name(self):
        return getattr(self._extcmd, 'baked_cmdName', None)

    @property
    def bundle(self):
        return getattr(self._extcmd, 'baked_cmdBundle', None)

    @property
    def extension(self):
        return getattr(self._extcmd, 'baked_cmdExtension', None)

    @property
    def unique_id(self):
        return getattr(self._extcmd, 'baked_cmdUniqueName', None)

    @property
    def needs_clean_engine(self):
        return getattr(self._extcmd, 'baked_needsCleanEngine', None)

    @property
    def needs_fullframe_engine(self):
        return getattr(self._extcmd, 'baked_needsFullFrameEngine', None)

    def is_available(self, category_set, zerodoc=False):
        if self._extcmd_availtype:
            return self._extcmd_avail.IsCommandAvailable(HOST_APP.uiapp,
                                                         category_set)
        elif not zerodoc:
            return True

        return False


pyrevit_extcmdtype_cache = []


def find_all_commands(category_set=None, cache=True):
    global pyrevit_extcmdtype_cache

    if cache and pyrevit_extcmdtype_cache:
        pyrevit_extcmds = pyrevit_extcmdtype_cache
    else:
        pyrevit_extcmds = []
        for loaded_assm_name in sessioninfo.get_loaded_pyrevit_assemblies():
            loaded_assm = coreutils.find_loaded_asm(loaded_assm_name)
            if loaded_assm:
                all_exported_types = loaded_assm[0].GetTypes()
                all_exported_type_names = [x.Name for x in all_exported_types]

                for pyrvt_type in all_exported_types:
                    tname = pyrvt_type.FullName
                    availtname = pyrvt_type.Name \
                                 + COMMAND_AVAILABILITY_NAME_POSTFIX
                    pyrvt_availtype = None

                    if not tname.endswith(COMMAND_AVAILABILITY_NAME_POSTFIX)\
                            and LOADER_BASE_NAMESPACE not in tname:
                        for exported_type in all_exported_types:
                            if exported_type.Name == availtname:
                                pyrvt_availtype = exported_type

                        pyrevit_extcmds.append(
                            PyRevitExternalCommandType(pyrvt_type,
                                                       pyrvt_availtype)
                            )
        if cache:
            pyrevit_extcmdtype_cache = pyrevit_extcmds

    # now check commands in current context if requested
    if category_set:
        return [x for x in pyrevit_extcmds
                if x.is_available(category_set=category_set,
                                  zerodoc=HOST_APP.uidoc is None)]
    else:
        return pyrevit_extcmds


def find_all_available_commands(use_current_context=True, cache=True):
    if use_current_context:
        cset = revit.get_selection_category_set()
    else:
        cset = None

    return find_all_commands(category_set=cset, cache=cache)


def find_pyrevitcmd(pyrevitcmd_unique_id):
    """Searches the pyRevit-generated assemblies under current session for
    the command with the matching unique name (class name) and returns the
    command type. Notice that this returned value is a 'type' and should be
    instantiated before use.

    Example:
        >>> cmd = find_pyrevitcmd('pyRevitCorepyRevitpyRevittoolsReload')
        >>> command_instance = cmd()
        >>> command_instance.Execute() # Provide commandData, message, elements

    Args:
        pyrevitcmd_unique_id (str): Unique name for the command

    Returns:
        Type for the command with matching unique name
    """
    # go through assmebles loaded under current pyRevit session
    # and try to find the command
    logger.debug('Searching for pyrevit command: {}'
                 .format(pyrevitcmd_unique_id))
    for loaded_assm_name in sessioninfo.get_loaded_pyrevit_assemblies():
        logger.debug('Expecting assm: {}'.format(loaded_assm_name))
        loaded_assm = coreutils.find_loaded_asm(loaded_assm_name)
        if loaded_assm:
            logger.debug('Found assm: {}'.format(loaded_assm_name))
            for pyrvt_type in loaded_assm[0].GetTypes():
                logger.debug('Found Type: {}'.format(pyrvt_type))
                if pyrvt_type.FullName == pyrevitcmd_unique_id:
                    logger.debug('Found pyRevit command in {}'
                                 .format(loaded_assm_name))
                    return pyrvt_type
            logger.debug('Could not find pyRevit command.')
        else:
            logger.debug('Can not find assm: {}'.format(loaded_assm_name))

    return None


def create_tmp_commanddata():
    tmp_cmd_data = \
        FormatterServices.GetUninitializedObject(UI.ExternalCommandData)
    tmp_cmd_data.Application = HOST_APP.uiapp
    # tmp_cmd_data.IsReadOnly = False
    # tmp_cmd_data.View = None
    # tmp_cmd_data.JournalData = None
    return tmp_cmd_data


def execute_command_cls(extcmd_type, arguments=None,
                        clean_engine=False, fullframe_engine=False,
                        alternate_mode=False):

    command_instance = extcmd_type()
    # this is a manual execution from python code and not by user
    command_instance.executedFromUI = False
    # pass the arguments to the instance
    if arguments:
        command_instance.argumentList = Array[str](arguments)
    # force using clean engine
    command_instance.baked_needsCleanEngine = clean_engine
    # force using fullframe engine
    command_instance.baked_needsFullFrameEngine = fullframe_engine
    # force using the alternate script
    command_instance.altScriptModeOverride = alternate_mode

    re = command_instance.Execute(create_tmp_commanddata(),
                                  '',
                                  DB.ElementSet())
    command_instance = None
    return re


def execute_command(pyrevitcmd_unique_id):
    """Executes a pyRevit command.

    Args:
        pyrevitcmd_unique_id (str): Unique/Class Name of the pyRevit command

    Returns:
        results from the executed command
    """

    cmd_class = find_pyrevitcmd(pyrevitcmd_unique_id)

    if not cmd_class:
        logger.error('Can not find command with unique name: {}'
                     .format(pyrevitcmd_unique_id))
        return None
    else:
        execute_command_cls(cmd_class)


def execute_script(script_path, arguments=None,
                   clean_engine=True, fullframe_engine=True):
    """Executes a script using pyRevit script executor.

    Args:
        script_path (str): Address of the script file

    Returns:
        results dictionary from the executed script
    """

    from pyrevit import MAIN_LIB_DIR, MISC_LIB_DIR
    from pyrevit.coreutils import loadertypes
    from pyrevit.coreutils import DEFAULT_SEPARATOR
    from pyrevit.framework import clr

    executor = loadertypes.ScriptExecutor()
    script_name = op.basename(script_path)
    sys_paths = DEFAULT_SEPARATOR.join([MAIN_LIB_DIR,
                                        MISC_LIB_DIR])

    cmd_runtime = \
        loadertypes.PyRevitCommandRuntime(
            cmdData=create_tmp_commanddata(),
            elements=None,
            scriptSource=script_path,
            alternateScriptSource=None,
            syspaths=sys_paths,
            arguments=arguments,
            helpSource=None,
            cmdName=script_name,
            cmdBundle=None,
            cmdExtension=None,
            cmdUniqueName=None,
            needsCleanEngine=clean_engine,
            needsFullFrameEngine=fullframe_engine,
            refreshEngine=False,
            forcedDebugMode=False,
            altScriptMode=False,
            executedFromUI=False
            )

    executor.ExecuteScript(
        clr.Reference[loadertypes.PyRevitCommandRuntime](cmd_runtime)
        )

    return cmd_runtime.GetResultsDictionary()
