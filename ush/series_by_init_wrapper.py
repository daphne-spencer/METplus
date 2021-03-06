#!/usr/bin/env python

from __future__ import print_function

import errno
import os
import re
import sys

import config_metplus
import met_util as util
import produtil.setup
from command_builder import CommandBuilder
from produtil.run import batchexe
from produtil.run import run

'''! @namespace SeriesByInitWrapper
@brief Performs any optional filtering of input tcst data then performs
regridding via either MET regrid_data_plane or wgrib2, then builds up
the commands to perform a series analysis by init time by invoking the
MET tool series_analysis. NetCDF plots are generated by invoking the MET tool
plot_data_plane. The NetCDF plots are then converted to .png and Postscript.

Call as follows:
@code{.sh}
series_by_init.py [-c /path/to/user.template.conf]
@endcode
'''


class SeriesByInitWrapper(CommandBuilder):
    """!  Performs series analysis based on init time by first performing any
          additional filtering via the wrapper to the MET tool tc_stat,
          tc_stat_wrapper.  Next, the arguments to run the MET tool
          series_analysis is done
    """

    def __init__(self, p, logger):
        super(SeriesByInitWrapper, self).__init__(p, logger)
        # Retrieve any necessary values (dirs, executables)
        # from the param file(s)

        self.p = p
        self.logger = logger
        self.var_list = util.getlist(p.getstr('config', 'VAR_LIST'))
        self.stat_list = util.getlist(p.getstr('config', 'STAT_LIST'))

        self.regrid_with_met_tool = p.getbool('config',
                                              'REGRID_USING_MET_TOOL')
        self.extract_tiles_dir = p.getdir('EXTRACT_OUT_DIR')
        self.series_out_dir = p.getdir('SERIES_INIT_OUT_DIR')
        self.series_filtered_out_dir = \
            p.getdir('SERIES_INIT_FILTERED_OUT_DIR')
        self.filter_opts = \
            p.getstr('config', 'SERIES_ANALYSIS_FILTER_OPTS')
        self.fcst_ascii_file_prefix = 'FCST_ASCII_FILES_'
        self.anly_ascii_file_prefix = 'ANLY_ASCII_FILES_'

        # Needed for generating plots
        self.sbi_plotting_out_dir = ''

        # For building the argument string via
        # CommandBuilder:
        met_build_base = p.getdir('MET_BUILD_BASE')
        self.app_path = os.path.join(met_build_base, 'bin/series_analysis')
        self.app_name = os.path.basename(self.app_path)
        self.inaddons = []
        self.infiles = []
        self.outdir = ""
        self.outfile = ""
        self.args = []

    def run_all_times(self):
        """! Invoke the series analysis script based on
            the init time in the format YYYYMMDD_hh

            Args:

            Returns:
                None:  Creates graphical plots of storm tracks
        """
        # pylint:disable=protected-access
        # Need to call sys.__getframe() to get the filename and method/func
        # for logging information.

        # Used for logging.
        cur_filename = sys._getframe().f_code.co_filename
        cur_function = sys._getframe().f_code.co_name
        self.logger.info("INFO|:" + cur_function + '|' + cur_filename + '| ' +
                         "Starting series analysis by init time")

        # Set up the environment variable to be used in the Series Analysis
        #   Config file (SERIES_ANALYSIS_BY_INIT_CONFIG_FILE)
        # Used to set cnt  value in output_stats in
        # "SERIES_ANALYSIS_BY_INIT_CONFIG_FILE"
        # Need to do some pre-processing so that Python will use " and not '
        #  because currently MET doesn't support single-quotes
        tmp_stat_string = str(self.stat_list)
        tmp_stat_string = tmp_stat_string.replace("\'", "\"")

        # For example, we want tmp_stat_string to look like
        #   '["TOTAL","FBAR"]', NOT "['TOTAL','FBAR']"
        os.environ['STAT_LIST'] = tmp_stat_string
        self.add_env_var('STAT_LIST', tmp_stat_string)

        series_filter_opts = \
            self.p.getstr('config', 'SERIES_ANALYSIS_FILTER_OPTS')

        if self.regrid_with_met_tool:
            # Regridding via MET Tool regrid_data_plane.
            fcst_tile_regex = self.p.getstr('regex_pattern',
                                            'FCST_NC_TILE_REGEX')
            anly_tile_regex = self.p.getstr('regex_pattern',
                                            'ANLY_NC_TILE_REGEX')
        else:
            # Regridding via wgrib2 tool.
            fcst_tile_regex = self.p.getstr('regex_pattern',
                                            'FCST_TILE_REGEX')
            anly_tile_regex = self.p.getstr('regex_pattern',
                                            'ANLY_TILE_REGEX')
        # Initialize the tile_dir to point to the extract_tiles_dir.
        # And retrieve a list of init times based on the data available in
        # the extract tiles directory.
        tile_dir = self.extract_tiles_dir
        init_times = util.get_updated_init_times(tile_dir, self.logger)

        # Check for input tile data.
        try:
            util.check_for_tiles(tile_dir, fcst_tile_regex,
                                 anly_tile_regex, self.logger)
        except OSError:
            msg = ("Missing n x m tile files.  " +
                   "Extract tiles needs to be run")
            self.logger.error(msg)

        # If applicable, apply any filtering via tc_stat, as indicated in the
        # parameter/config file.
        tmp_dir = os.path.join(self.p.getdir('TMP_DIR'), str(os.getpid()))
        if series_filter_opts:
            util.apply_series_filters(tile_dir, init_times,
                                      self.series_filtered_out_dir,
                                      self.filter_opts,
                                      tmp_dir, self.logger, self.p)

            # Clean up any empty files and directories that could arise as
            # a result of filtering
            util.prune_empty(self.series_filtered_out_dir, self.logger)

            # Get the list of all the files that were created as a result
            # of applying the filter options.
            # First, make sure that the series_lead_filtered_out
            # directory isn't empty.  If so, then no files fall within the
            # filter criteria.
            if os.listdir(self.series_filtered_out_dir):
                # The series filter directory has data, use this directory as
                # input for series analysis.
                tile_dir = self.series_filtered_out_dir

                # Generate the tmp_anly and tmp_fcst files used to validate
                # filtering and for troubleshooting
                # The tmp_fcst and tmp_anly ASCII files contain the
                # list of files that meet the filter criteria.
                filtered_dirs_list = util.get_files(tile_dir, ".*.",
                                                    self.logger)
                util.create_filter_tmp_files(filtered_dirs_list,
                                             self.series_filtered_out_dir,
                                             self.logger)

            else:
                # Applying the filter produced no results.  Rather than
                # stopping, continue by using the files from extract_
                # tiles as input.
                msg = ("INFO| Applied series filter options, no results..." +
                       "using extract tiles data for series analysis input.")
                self.logger.debug(msg)
                tile_dir = self.extract_tiles_dir

        else:
            # No additional filtering was requested.
            # Use the data in the extract tiles directory
            # as input for series analysis.
            # source of input tile data.
            tile_dir = self.extract_tiles_dir

        # Create FCST and ANLY ASCII files based on init time and storm id.
        # These are arguments to the
        # -fcst and -obs arguments to the MET Tool series_analysis.
        # First, get an updated list of init times,
        # since filtering can reduce the amount of init times.
        sorted_filter_init = self.get_ascii_storm_files_list(tile_dir)

        # Clean up any remaining empty files and dirs
        util.prune_empty(self.series_out_dir, self.logger)
        self.logger.debug("Finished creating FCST and ANLY ASCII files, and " +
                          "cleaning empty files and dirs")

        # Build up the arguments to and then run the MET tool series_analysis.
        self.build_and_run_series_request(sorted_filter_init, tile_dir)

        # Generate plots
        # Check for .nc files in output_dir first, if these are absent, the
        # there is a problem.
        if self.is_netcdf_created():
            self.generate_plots(sorted_filter_init, tile_dir)
        else:
            self.logger.error("ERROR|:" + cur_function + "|" + cur_filename +
                              "No NetCDF files were created by"
                              " series_analysis, exiting...")
            sys.exit(errno.ENODATA)
        self.logger.info("INFO|" + "Finished series analysis by init time")

    def is_netcdf_created(self):
        """! Check for the presence of NetCDF files in the series_analysis_init
             directory

             Returns:
                 is_created         True if NetCDF files were found in the
                                    series_analysis_init/YYYYMMDD_hh/storm
                                    sub-directories, False otherwise.
        """
        dated_dir_list = os.listdir(self.series_out_dir)
        netcdf_file_counter = 0
        is_created = False

        # Get the storm sub-directories in each dated sub-directory
        for dated_dir in dated_dir_list:
            dated_dir_path = os.path.join(self.series_out_dir, dated_dir)
            # Get a list of the storm sub-dirs in this directory
            all_storm_list = os.listdir(dated_dir_path)
            for each_storm in all_storm_list:
                full_storm_dirname = os.path.join(dated_dir_path, each_storm)
                # Now get the list of files for each storm sub-dir.
                all_files = os.listdir(full_storm_dirname)
                for each_file in all_files:
                    full_filepath = os.path.join(full_storm_dirname, each_file)
                    if os.path.isfile(full_filepath):
                        if full_filepath.endswith('.nc'):
                            netcdf_file_counter += 1

        if netcdf_file_counter > 0:
            is_created = True

        return is_created

    def get_fcst_file_info(self, dir_to_search, cur_init, cur_storm):
        """! Get the number of all the gridded forecast n x m tile
            files for a given storm id and init time
            (that were created by extract_tiles). Determine the filename of the
            first and last files.  This information is used to create
            the title value to the -title opt in plot_data_plane.

            Args:
            @param dir_to_search: The directory of the gridded files of
                                  interest.
            @param cur_init:  The init time of interest.
            @param cur_storm:  The storm id of interest.

            Returns:
            num, beg, end:  A tuple representing the number of
                            forecast tile files, and the first and
                            last file.

                            sys.exit(1) otherwise
        """

        # pylint:disable=protected-access
        # Need to call sys.__getframe() to get the filename and method/func
        # for logging information.
        # For logging
        cur_filename = sys._getframe().f_code.co_filename
        cur_function = sys._getframe().f_code.co_name

        # Get a sorted list of the forecast tile files for the init
        # time of interest for all the storm ids and return the
        # forecast hour corresponding to the first and last file.
        # base_dir_to_search = os.path.join(output_dir, cur_init)
        gridded_dir = os.path.join(dir_to_search, cur_init, cur_storm)
        search_regex = ".*FCST_TILE.*.grb2"

        if self.regrid_with_met_tool:
            search_regex = ".*FCST_TILE.*.nc"

        files_of_interest = util.get_files(gridded_dir, search_regex,
                                           self.logger)
        sorted_files = sorted(files_of_interest)
        if not files_of_interest:
            msg = ("ERROR:|[" + cur_filename + ":" +
                   cur_function + "]|exiting, no files found for " +
                   "init time of interest" +
                   " and directory:" + dir_to_search)
            self.logger.error(msg)
            sys.exit(1)

        first = sorted_files[0]
        last = sorted_files[-1]

        # Extract the forecast hour from the first and last
        # filenames.
        match_beg = re.search(".*FCST_TILE_(F[0-9]{3}).*.grb2", first)
        match_end = re.search(".*FCST_TILE_(F[0-9]{3}).*.grb2", last)
        if self.regrid_with_met_tool:
            match_beg = re.search(".*FCST_TILE_(F[0-9]{3}).*.nc", first)
            match_end = re.search(".*FCST_TILE_(F[0-9]{3}).*.nc", last)
        if match_beg:
            beg = match_beg.group(1)
        else:
            msg = ("ERROR|[" + cur_filename + ":" + cur_function + "]| " +
                   "Unexpected file format encountered, exiting...")
            self.logger.error(msg)
            sys.exit(1)
        if match_end:
            end = match_end.group(1)
        else:
            msg = ("ERROR|[" + cur_filename + ":" + cur_function +
                   "]| " +
                   "Unexpected file format encountered, exiting...")
            self.logger.error(msg)
            sys.exit(1)

        # Get the number of forecast tile files
        num = len(sorted_files)

        return num, beg, end

    def get_ascii_storm_files_list(self, tile_dir):
        """! Creates the list of ASCII files that contain the storm id and init
             times.  The list is used to create an ASCII file which will be
             used as the option to the -obs or -fcst flag to the MET
             series_analysis tool.
             Args:
                   @param tile_dir:  The directory where input files reside.
             Returns:
                   sorted_filter_init:  A list of the sorted directories
                                        corresponding to the init times after
                                        filtering has been applied.  If
                                        filtering produced no results, this
                                        is the list of files created from
                                        running extract_tiles.
        """

        # pylint:disable=protected-access
        # Need to call sys.__getframe() to get the filename and method/func
        # for logging information.
        # For logging
        cur_filename = sys._getframe().f_code.co_filename
        cur_function = sys._getframe().f_code.co_name
        self.logger.debug("DEBUG|" + cur_function + '|' + cur_filename)

        filter_init_times = util.get_updated_init_times(tile_dir, self.logger)
        sorted_filter_init = sorted(filter_init_times)

        for cur_init in sorted_filter_init:
            # Get all the storm ids for storm track pairs that
            # correspond to this init time.
            storm_list = self.get_storms_for_init(cur_init, tile_dir)
            if not storm_list:
                # No storms for this init time,
                # check next init time in list
                continue
            else:
                for cur_storm in storm_list:
                    # First get the filenames for the gridded forecast and
                    # analysis (n deg x m deg tiles that were created by
                    # extract_tiles). These files are aggregated by
                    # init time and storm id.
                    anly_grid_regex = ".*ANLY_TILE_F.*grb2"
                    fcst_grid_regex = ".*FCST_TILE_F.*grb2"

                    if self.regrid_with_met_tool:
                        anly_grid_regex = ".*ANLY_TILE_F.*nc"
                        fcst_grid_regex = ".*FCST_TILE_F.*nc"

                    anly_grid_files = util.get_files(tile_dir,
                                                     anly_grid_regex,
                                                     self.logger)
                    fcst_grid_files = util.get_files(tile_dir,
                                                     fcst_grid_regex,
                                                     self.logger)

                    # Now do some checking to make sure we aren't
                    # missing either the forecast or
                    # analysis files, if so log the error and proceed to next
                    # storm in the list.
                    if not anly_grid_files or not fcst_grid_files:
                        # No gridded analysis or forecast
                        # files found, continue
                        self.logger.info("INFO|:" + cur_function + "|" +
                                         cur_filename + "| " +
                                         "no gridded analysis or forecast " +
                                         "file found, continue to next storm")
                        continue

                    # Now create the FCST and ANLY ASCII files based on
                    # cur_init and cur_storm:
                    self.create_fcst_anly_to_ascii_file(
                        fcst_grid_files, cur_init, cur_storm,
                        self.fcst_ascii_file_prefix)
                    self.create_fcst_anly_to_ascii_file(
                        anly_grid_files, cur_init, cur_storm,
                        self.anly_ascii_file_prefix)
                    util.prune_empty(self.series_out_dir, self.logger)
        return sorted_filter_init

    def build_and_run_series_request(self, sorted_filter_init, tile_dir):
        """! Build up the -obs, -fcst, -out necessary for running the
             series_analysis MET tool, then invoke series_analysis.

             Args:
                  @param sorted_filter_init:  A list of the sorted directories
                                        corresponding to the init times that
                                        are the result of filtering.  If
                                        filtering produced no results, this
                                        is the list of files created from
                                        running extract_tiles.
                  @param tile_dir:  The directory where the input resides.
             Returns:
        """

        # pylint:disable=protected-access
        # Need to call sys.__getframe() to get the filename and method/func
        # for logging information.
        # For logging
        cur_filename = sys._getframe().f_code.co_filename
        cur_function = sys._getframe().f_code.co_name
        self.logger.debug("DEBUG|" + cur_function + '|' + cur_filename)

        # Now assemble the -fcst, -obs, and -out arguments and invoke the
        # MET Tool: series_analysis.
        for cur_init in sorted_filter_init:
            storm_list = self.get_storms_for_init(cur_init, tile_dir)
            for cur_storm in storm_list:
                if not storm_list:
                    # No storm ids found for cur_init
                    # check next init time in the list.
                    continue
                else:
                    # Build the -obs and -fcst portions of the series_analysis
                    # command. Then generate the -out portion, get the NAME and
                    # corresponding LEVEL for each variable.
                    for cur_var in self.var_list:
                        name, level = util.get_name_level(cur_var, self.logger)
                        param = \
                            self.p.getstr(
                                'config',
                                'SERIES_ANALYSIS_BY_INIT_CONFIG_FILE')
                        self.set_param_file(param)
                        self.create_obs_fcst_arg('obs',
                                                 self.anly_ascii_file_prefix,
                                                 cur_storm, cur_init)
                        self.create_obs_fcst_arg('fcst',
                                                 self.fcst_ascii_file_prefix,
                                                 cur_storm,
                                                 cur_init)
                        self.create_out_arg(cur_storm, cur_init, name, level)
                        self.build()
                        self.clear()

    def create_obs_fcst_arg(self, param_arg, ascii_file_base, cur_storm,
                            cur_init):
        """! Create the argument to the -obs or -fcst flag to the MET tool,
             series_analysis.

             Args:
                 @param param_arg:  '-obs' for the -obs argument to
                                    series_analysis, or '-fcst' for the
                                    -fcst argument to series_analysis.
                 @param ascii_file_base:  The base name of the obs or fcst
                                          ASCII file that contains a list
                                          of the full filepath to the input
                                          files.

                 @param cur_storm:  The storm of interest.

                 @param cur_init:   The initialization time of interest.

             Returns:
        """

        # pylint:disable=protected-access
        # Need to call sys.__getframe() to get the filename and method/func
        # for logging information.
        # For logging
        cur_filename = sys._getframe().f_code.co_filename
        cur_function = sys._getframe().f_code.co_name
        ascii_fname_parts = [ascii_file_base, cur_storm]
        ascii_fname = ''.join(ascii_fname_parts)
        ascii_full_path = os.path.join(self.series_out_dir, cur_init,
                                       cur_storm, ascii_fname)
        self.add_input_file(ascii_full_path, param_arg)
        self.get_input_files()
        latest_idx = len(self.get_input_files()) - 1
        msg = \
            ("DEBUG|[" + cur_function + ":" + cur_filename + "]" +
             "first param: " + self.get_input_files()[latest_idx])
        self.logger.debug(msg)

    def create_out_arg(self, cur_storm, cur_init, name, level):
        """! Create/build the -out portion of the series_analysis command and
             creates the output directory.
            Args:
                @param cur_storm: The storm of interest.

                @param cur_init:  The initialization time of interest.

                @param name:  The variable name of interest.

                @param level:  The level of interest.

            Returns:
        """

        # pylint:disable=protected-access
        # Need to call sys.__getframe() to get the filename and method/func
        # for logging information.
        # For logging
        cur_filename = sys._getframe().f_code.co_filename
        cur_function = sys._getframe().f_code.co_name

        # create the output dir
        self.outdir = os.path.join(self.series_out_dir, cur_init,
                                   cur_storm)
        util.mkdir_p(self.outdir)
        # Set the NAME and LEVEL environment variables, this
        # is required by the MET series_analysis binary.
        os.environ['NAME'] = name
        os.environ['LEVEL'] = level
        self.add_env_var('NAME', name)
        self.add_env_var('LEVEL', level)

        # Set the NAME to name_level if regrid_data_plane
        # was used to regrid.
        if self.regrid_with_met_tool:
            name_level = name + "_" + level
            os.environ['NAME'] = name_level
            self.add_env_var('NAME', name_level)
        series_anly_output_parts = [self.outdir, '/',
                                    'series_', name, '_',
                                    level, '.nc']
        # Set the sbi_out_dir for this instance, this will be
        # used for generating the plot.
        self.sbi_plotting_out_dir = ''.join(
            series_anly_output_parts)
        self.outfile = self.sbi_plotting_out_dir

        self.logger.debug("DEBUG|" + cur_function + '|' + cur_filename +
                          '| output arg/output dir for series_analysis: ' +
                          self.get_output_path())
        self.set_output_dir(self.outdir)
        self.set_output_filename(self.outfile)

    def clear(self):
        super(SeriesByInitWrapper, self).clear()
        self.inaddons = []

    def add_input_file(self, filename, type_id):
        self.infiles.append(filename)
        self.inaddons.append("-" + type_id)

    def get_command(self):
        if self.app_path is None:
            self.logger.error("No app path specified. You must use a subclass")
            return None

        cmd = self.app_path + " "
        if self.args:
            for a in self.args:
                cmd += a + " "

        # if len(self.infiles) == 0:
        #     self.logger.error("No input filenames specified")
        #     return None

        for idx, f in enumerate(self.infiles):
            cmd += self.inaddons[idx] + " " + f + " "

        if self.param != "":
            cmd += "-config " + self.param + " "

        if self.get_output_path() == "":
            self.logger.error("No output directory specified")
            self.logger.error("No output filename specified")
            return None
        else:
            cmd += "-out " + os.path.join(self.get_output_path())
        self.logger.debug("DEBUG|Command= " + cmd)
        return cmd

    def generate_plots(self, sorted_filter_init, tile_dir):
        """! Generate the plots from the series_analysis output.
           Args:
               @param sorted_filter_init:  A list of the sorted directories
                                        corresponding to the init times (that
                                        are the result of filtering).  If
                                        filtering produced no results, this
                                        is the list of files created from
                                        running extract_tiles.

               @param tile_dir:  The directory where input data resides.
           Returns:
        """
        convert_exe = self.p.getexe('CONVERT_EXE')
        background_map = self.p.getbool('config', 'BACKGROUND_MAP')
        plot_data_plane_exe = os.path.join(self.p.getdir('MET_BUILD_BASE'),
                                           'bin/plot_data_plane')
        for cur_var in self.var_list:
            name, level = util.get_name_level(cur_var, self.logger)
            for cur_init in sorted_filter_init:
                storm_list = self.get_storms_for_init(cur_init, tile_dir)
                for cur_storm in storm_list:
                    # create the output directory where the finished
                    # plots will reside
                    output_dir = os.path.join(self.series_out_dir, cur_init,
                                              cur_storm)
                    util.mkdir_p(output_dir)

                    # Now we need to invoke the MET tool
                    # plot_data_plane to generate plots that are
                    # recognized by the MET viewer.
                    # Get the number of forecast tile files,
                    # the name of the first and last in the list
                    # to be used by the -title option.
                    if tile_dir == self.extract_tiles_dir:
                        # Since filtering was not requested, or
                        # the additional filtering doesn't yield results,
                        # search the series_out_dir
                        num, beg, end = \
                            self.get_fcst_file_info(self.series_out_dir,
                                                    cur_init, cur_storm)
                    else:
                        # Search the series_filtered_out_dir for
                        # the filtered files.
                        num, beg, end = self.get_fcst_file_info(
                            self.series_filtered_out_dir, cur_init,
                            cur_storm)

                    # Assemble the input file, output file, field string,
                    # and title
                    plot_data_plane_input_fname = self.sbi_plotting_out_dir
                    for cur_stat in self.stat_list:
                        plot_data_plane_output = [output_dir,
                                                  '/series_',
                                                  name, '_',
                                                  level, '_',
                                                  cur_stat, '.ps']
                        plot_data_plane_output_fname = ''.join(
                            plot_data_plane_output)
                        os.environ['CUR_STAT'] = cur_stat
                        self.add_env_var('CUR_STAT', cur_stat)

                        # Create versions of the arg based on
                        # whether the background map is requested
                        # in param file.
                        map_data = ' map_data={ source=[];}'

                        if background_map:
                            # Flag set to True, draw background map.
                            field_string_parts = ["'name=", '"series_cnt_',
                                                  cur_stat, '";',
                                                  'level="', level, '";',
                                                  "'"]
                        else:
                            field_string_parts = ["'name=", '"series_cnt_',
                                                  cur_stat, '";',
                                                  'level="', level, '";',
                                                  map_data, "'"]

                        field_string = ''.join(field_string_parts)
                        title_parts = [' -title "GFS Init ', cur_init,
                                       ' Storm ', cur_storm, ' ',
                                       str(num), ' Forecasts (',
                                       str(beg), ' to ', str(end),
                                       '),', cur_stat, ' for ',
                                       cur_var, '"']
                        title = ''.join(title_parts)

                        # Now assemble the entire plot data plane command
                        data_plane_command_parts = \
                            [plot_data_plane_exe, ' ',
                             plot_data_plane_input_fname, ' ',
                             plot_data_plane_output_fname, ' ',
                             field_string, ' ', title]

                        data_plane_command = ''.join(
                            data_plane_command_parts)
                        data_plane_command = \
                            batchexe('sh')[
                                '-c', data_plane_command].err2out()
                        run(data_plane_command)

                        # Now assemble the command to convert the
                        # postscript file to png
                        png_fname = plot_data_plane_output_fname.replace(
                            '.ps', '.png')
                        convert_parts = [convert_exe, ' -rotate 90',
                                         ' -background white -flatten ',
                                         plot_data_plane_output_fname,
                                         ' ', png_fname]
                        convert_command = ''.join(convert_parts)
                        convert_command = \
                            batchexe('sh')['-c', convert_command].err2out()
                        run(convert_command)

    def get_storms_for_init(self, cur_init, out_dir_base):
        """! Retrieve all the filter files which have the .tcst
             extension.  Inside each file, extract the STORM_ID
             and append to the list, if the storm_list directory
             exists.

            Args:
              @param cur_init: the init time

              @param out_dir_base:  The directory where one should start
                                    searching for the filter file(s)
                                    - those with a .tcst file extension.


        Returns:
            storm_list: A list of all the storms ids that correspond to
                        this init time and actually has a directory in the
                        init dir (additional filtering in a previous step
                        may result in missing storm ids even though they are
                        in the filter.tcst file)
        """

        # pylint:disable=protected-access
        # Need to call sys.__getframe() to get the filename and method/func
        # for logging information.
        # For logging
        cur_filename = sys._getframe().f_code.co_filename
        cur_function = sys._getframe().f_code.co_name
        self.logger.debug("DEBUG|" + cur_function + '|' + cur_filename)

        # Retrieve filter files, first create the filename
        # by piecing together the out_dir_base with the cur_init.
        filter_filename = "filter_" + cur_init + ".tcst"
        filter_file = os.path.join(out_dir_base, cur_init, filter_filename)

        # Now that we have the filter filename for the init time, let's
        # extract all the storm ids in this filter file.
        storm_list = util.get_storm_ids(filter_file, self.logger)

        return storm_list

    def create_fcst_anly_to_ascii_file(self, fcst_anly_grid_files, cur_init,
                                       cur_storm, fcst_anly_filename_base):
        """! Create ASCII file for either the FCST or ANLY files that are
             aggregated based on init time and storm id.

        Args:
            fcst_anly_grid_files:       A list of the FCST or ANLY gridded
                                        files under consideration.

            cur_init:                  The initialization time of interest

            cur_storm:                 The storm id of interest

            fcst_anly_filename_base:   The base name of the ASCII file
                                        (either ANLY_ASCII_FILES_ or
                                        FCST_ASCII_FILES_ which will be
                                        appended with the storm id.

        Returns:
            None:                      Creates an ASCII file containing a list
                                        of either FCST or ANLY files based on
                                        init time and storm id.
        """

        # pylint:disable=protected-access
        # Need to call sys.__getframe() to get the filename and method/func
        # for logging information.

        # For logging
        cur_filename = sys._getframe().f_code.co_filename
        cur_function = sys._getframe().f_code.co_name

        # Create an ASCII file containing a list of all
        # the fcst or analysis tiles.
        fcst_anly_ascii_fname_parts = [fcst_anly_filename_base, cur_storm]
        fcst_anly_ascii_fname = ''.join(fcst_anly_ascii_fname_parts)
        fcst_anly_ascii_dir = os.path.join(self.series_out_dir, cur_init,
                                           cur_storm)
        util.mkdir_p(fcst_anly_ascii_dir)
        fcst_anly_ascii = os.path.join(fcst_anly_ascii_dir,
                                       fcst_anly_ascii_fname)

        # Sort the files in the fcst_anly_grid_files list.
        sorted_fcst_anly_grid_files = sorted(fcst_anly_grid_files)
        tmp_param = ''
        for cur_fcst_anly in sorted_fcst_anly_grid_files:
            # Write out the files that pertain to this storm and
            # don't write if already in tmp_param.
            if cur_fcst_anly not in tmp_param and cur_storm in cur_fcst_anly:
                tmp_param += cur_fcst_anly
                tmp_param += '\n'
        # Now create the fcst or analysis ASCII file
        try:
            with open(fcst_anly_ascii, 'a') as filehandle:
                filehandle.write(tmp_param)
        except IOError:
            msg = ("ERROR|[" + cur_filename + ":" +
                   cur_function + "]| " +
                   "Could not create requested ASCII file:  " +
                   fcst_anly_ascii)
            self.logger.error(msg)

        if os.stat(fcst_anly_ascii).st_size == 0:
            # Just in case there are any empty fcst ASCII or anly ASCII files
            # at this point,
            # explicitly remove them (and any resulting empty directories)
            #  so they don't cause any problems with further processing
            # steps.
            util.prune_empty(fcst_anly_ascii_dir, self.logger)


if __name__ == "__main__":
    try:
        if 'JLOGFILE' in os.environ:
            produtil.setup.setup(send_dbn=False, jobname='SeriesByInit',
                                 jlogfile=os.environ['JLOGFILE'])
        else:
            produtil.setup.setup(send_dbn=False, jobname='SeriesByInit')
        produtil.log.postmsg('series_by_init is starting')

        # Read in the configuration object CONFIG
        CONFIG = config_metplus.setup()
        LOG = util.get_logger(CONFIG)
        if 'MET_BASE' not in os.environ:
            os.environ['MET_BASE'] = CONFIG.getdir('MET_BASE')

        SBI = SeriesByInitWrapper(CONFIG, LOG)
        SBI.run_all_times()
        produtil.log.postmsg('series_by_init completed')
    except Exception as e:
        produtil.log.jlogger.critical(
            'series_by_init failed: %s' % (str(e),), exc_info=True)
        sys.exit(2)
