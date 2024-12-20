from astroquery.linelists.cdms import CDMS
from astroquery.jplspec import JPLSpec as JPL
import astropy.units as u
import astropy.constants as ac
import numpy as np
from scipy.interpolate import interp1d
from astropy.io import ascii

h = ac.h.cgs.value
c = ac.c.cgs.value
k_B = ac.k_B.cgs.value


class PartitionFunction:
    def __init__(self, species, T, Q, database=None, ntrans=None):
        self.species = species
        self.T = T
        self.Q = Q
        self.database = database
        self.ntrans = ntrans

        self.function = self._get_function()

    def __call__(self, T, verbose=False):
        if verbose and (T < np.nanmin(self.T) or T > np.nanmax(self.T)):
            print(
                "Warning: Input temperature is smaller or larger than the original partition function data. Will be evaluated by extrapolation."
            )
        val = self.function(T)
        if val.size == 1:
            return float(val)
        else:
            return val

    def _get_function(self):
        T = self.T[~np.isnan(self.Q)]
        Q = self.Q[~np.isnan(self.Q)]
        return interp1d(T, Q, kind="cubic", fill_value="extrapolate")


def wavenumber_to_Kelvin(wavenumber):
    return wavenumber * h * c / k_B


def logint_to_EinsteinA(logint_300, nu0, gup, Elow, Q_300):
    """convert CDMS intensity at 300 K (in nm2 MHz) to Einstein A coeff. (in s-1).
    See https://cdms.astro.uni-koeln.de/classic/predictions/description.html#description for conversion equations

    Parameters
    ----------
    logint_300 : float or ndarray
        log10 CDMS intensity at 300 K in nm2 MHz
    nu0 : float
        line frequency in MHz
    gup : float
        upper state degeneracy
    Elow : float
        lower state energy in cm-1
    Q_300 : float
        partition function at 300 K

    Returns
    -------
    float or ndarray
        Einstein A coeff.
    """
    Elow = wavenumber_to_Kelvin(Elow)
    Eup = Elow + h * nu0 * 1e6 / k_B  # in K
    Smu2 = (
        2.40251e4
        * 10**logint_300
        * Q_300
        / nu0
        / (np.exp(-Elow / 300) - np.exp(-Eup / 300))
    )
    A = 1.16395e-20 * nu0**3 * Smu2 / gup
    return A


class SpectroscopicData:

    def __init__(self, filename=None, format=None, species=None, pf=None):
        self.filename = filename

        if filename is not None:
            self.parse_datafile(format=format, species=species, pf=pf)

    def _set_quantities(self):
        try:
            self.mu = self.table.meta["Molecular Weight"]
        except KeyError:
            pass
        self.Q = self.table.meta["Partition Function"]
        self.nu0 = self.table["Frequency"].value * 1e9
        self.Aul = self.table["A_ul"].value
        self.gup = self.table["g_up"].value
        self.Eup = self.table["E_up"].value

    
    # def add_partition_function(self, species_id):

    @staticmethod
    def read_JPL_partition_function(species_table, tag):
        row = species_table[species_table["TAG"] == tag]

        T = np.array(
            species_table.meta["Temperature (K)"][::-1]
        )  # reverse the order to be in increasing temperature order
        Q = 10 ** np.array(
            [float(row[k]) or np.nan for k in row.keys() if "QLOG" in k][::-1]
        )

        return T, Q

    @staticmethod
    def read_CDMS_partition_function(species_table, tag):
        row = species_table[species_table["tag"] == tag]

        T = np.array(
            [float(k.split("(")[-1].split(")")[0]) for k in row.keys() if "lg" in k]
        )
        Q = 10 ** np.array(
            [float(row[k][0]) or np.nan for k in row.keys() if "lg" in k]
        )

        return T, Q

    def format_JPL(self, response, species=None, nofreqerr=False, pf=None):

        # copy for subsequent modification
        self.table = response

        # clean up resulting response
        # 1. remove masked column
        if self.table.mask is not None:
            masked_columns = [
                col for col in self.table.colnames if np.all(self.table.mask[col])
            ]
            self.table.remove_columns(masked_columns)
        # 2. metadata (including partition function) if species is specified
        ## get the specie name which are added to metadata table
        if species is None:
            tag = abs(int(np.unique(self.table["TAG"])[0]))
            species_table = JPL.get_species_table()
            try:
                idx = species_table["TAG"].tolist().index(tag)
            except ValueError:
                raise ValueError(f"No entries found for species tag {tag}. Please specify ``species'' argument.")
        
            self.species = species_table["NAME"][idx]

            if pf is None:
                T, Q = self.read_JPL_partition_function(
                    species_table=species_table, tag=tag
                )
                self.table.meta["Partition Function"] = PartitionFunction(
                    species=self.species, T=T, Q=Q, ntrans=species_table["NLINE"]
                )
            else:
                self.table.meta["Partition Function"] = pf
        
        else:
            self.species = species
            if pf is None:
                raise ValueError("Please provide parition function (pf) which is needed to calculate A coeff.")
            self.table.meta["Partition Function"] = pf

        self.table.meta["Species"] = self.species

        # 2. remove unnecessary columns
        self.table.remove_columns(["DR", "TAG", "QNFMT"])
        if nofreqerr:
            self.table.remove_column("ERR")
        self.table.add_column(
            col=[self.species] * len(self.table), name="Species", index=0
        )

        # 3. some calculus to make table values useful
        # 3-1. rename frequency (and error) and to GHz
        self.table.rename_column("FREQ", "Frequency")
        self.table["Frequency"] *= 1e-3
        self.table["Frequency"].unit = u.GHz
        self.table["Frequency"].format = "{:.7f}"
        if not nofreqerr:
            self.table.rename_column("ERR", "Frequency Error")
            self.table["Frequency Error"] *= 1e-3
            self.table["Frequency Error"].unit = u.GHz
            self.table["Frequency Error"].format = "{:.7f}"

        # 3-2. A coeff to not log
        self.table.rename_column("LGINT", "A_ul")
        self.table["A_ul"] = logint_to_EinsteinA(
            logint_300=self.table["A_ul"],
            nu0=self.table["Frequency"] * 1e3,  # in MHz
            gup=self.table["GUP"],
            Elow=self.table["ELO"],
            Q_300=self.table.meta["Partition Function"](300),
        )
        self.table["A_ul"].format = "{:.4e}"

        # 3-3. E_low to E_up
        self.table.rename_column("ELO", "E_up")
        self.table["E_up"] = (
            wavenumber_to_Kelvin(self.table["E_up"])
            + h * self.table["Frequency"] * 1e9 / k_B
        )
        self.table["E_up"].unit = "K"
        self.table["E_up"].format = "{:.5f}"

        # 3-4. GUP to g_up
        self.table.rename_column("GUP", "g_up")

        # setup
        self._set_quantities()

    def format_CDMS(self, response, use_cached=False, nofreqerr=False):
        # copy for subsequent modification
        self.table = response

        # clean up resulting response
        # 1. remove masked column
        if self.table.mask is not None:
            masked_columns = [
                col for col in self.table.colnames if np.all(self.table.mask[col])
            ]
            self.table.remove_columns(masked_columns)
        # 2. metadata (including partition function) if species is specified
        ## get the specie name and molweight which are added to metadata table
        self.molweight = int(np.unique(self.table["MOLWT"])[0])
        tag = int(self.molweight * 1e3 + abs(int(np.unique(self.table["TAG"])[0])))
        self.species_table = CDMS.get_species_table(use_cached=use_cached)
        idx = self.species_table["tag"].tolist().index(tag)
        self.species = self.species_table["molecule"][idx]

        self.table.meta["Species"] = self.species
        self.table.meta["Molecular Weight"] = self.molweight

        # partition function
        T, Q = self.read_CDMS_partition_function(
            species_table=self.species_table, tag=tag
        )
        self.table.meta["Partition Function"] = PartitionFunction(
            species=self.species, T=T, Q=Q, ntrans=self.species_table["#lines"]
        )

        # 2. remove unnecessary columns
        self.table.remove_columns(["DR", "TAG", "QNFMT", "MOLWT", "Lab"])
        if nofreqerr:
            self.table.remove_column("ERR")
        self.table.add_column(col=self.table["name"], name="Species", index=0)
        self.table.remove_column("name")

        # 3. some calculus to make table values useful
        # 3-1. rename frequency (and error) and to GHz
        self.table.rename_column("FREQ", "Frequency")
        self.table["Frequency"] *= 1e-3
        self.table["Frequency"].unit = u.GHz
        self.table["Frequency"].format = "{:.7f}"
        if not nofreqerr:
            self.table.rename_column("ERR", "Frequency Error")
            self.table["Frequency Error"] *= 1e-3
            self.table["Frequency Error"].unit = u.GHz
            self.table["Frequency Error"].format = "{:.7f}"

        # 3-2. A coeff to not log
        self.table.rename_column("LGAIJ", "A_ul")
        self.table["A_ul"] = 10 ** self.table["A_ul"]
        self.table["A_ul"].format = "{:.4e}"

        # 3-3. E_low to E_up
        self.table.rename_column("ELO", "E_up")
        self.table["E_up"] = (
            wavenumber_to_Kelvin(self.table["E_up"])
            + h * self.table["Frequency"] * 1e9 / k_B
        )
        self.table["E_up"].unit = "K"
        self.table["E_up"].format = "{:.5f}"

        # 3-4. GUP to g_up
        self.table.rename_column("GUP", "g_up")

        # setup
        self._set_quantities()

    def query_JPL(self, freq_range=(0.0, np.inf), species_id=1001, nofreqerr=False):
        # frequency range in Hz
        numin, numax = freq_range

        # species
        response = JPL.query_lines(
            min_frequency=numin * u.Hz,
            max_frequency=numax * u.Hz,
            molecule=int(species_id),
        )

        if response is None:
            print("No lines found in the specified frequency range.")
            return

        self.format_JPL(response=response, nofreqerr=nofreqerr)

    def query_CDMS(
        self, freq_range=(0.0, np.inf), species_id="", use_cached=False, nofreqerr=False
    ):
        # frequency range
        numin, numax = freq_range

        # clear preivous caches
        CDMS.clear_cache()

        response = CDMS.query_lines(
            min_frequency=numin * u.Hz,
            max_frequency=numax * u.Hz,
            molecule=str(species_id).zfill(6),
            temperature_for_intensity=0,  # hack to retrieve A coeff instead of logint
        )

        if response is None:
            print("No lines found in the specified frequency range.")
            return

        self.format_CDMS(
            response=response,
            use_cached=use_cached,
            nofreqerr=nofreqerr,
        )

    def parse_datafile(self, format="JPL", species=None, pf=None):

        if format == "JPL":
            response = ascii.read(
                self.filename,
                header_start=None,
                data_start=0,
                names=(
                    "FREQ",
                    "ERR",
                    "LGINT",
                    "DR",
                    "ELO",
                    "GUP",
                    "TAG",
                    "QNFMT",
                    "QN'",
                    'QN"',
                ),
                col_starts=(0, 13, 21, 29, 31, 41, 44, 51, 55, 67),
                format="fixed_width",
                fast_reader=False,
            )

            self.format_JPL(response=response, species=species, pf=pf)

        elif format == "CDMS":
            starts = {
                "FREQ": 0,
                "ERR": 14,
                "LGINT": 25,
                "DR": 36,
                "ELO": 38,
                "GUP": 47,
                "MOLWT": 51,
                "TAG": 54,
                "QNFMT": 57,
                "Ju": 61,
                "Ku": 63,
                "vu": 65,
                "F1u": 67,
                "F2u": 69,
                "F3u": 71,
                "Jl": 73,
                "Kl": 75,
                "vl": 77,
                "F1l": 79,
                "F2l": 81,
                "F3l": 83,
                "name": 89,
            }

            response = ascii.read(
                self.filename,
                header_start=None,
                data_start=0,
                names=list(starts.keys()),
                col_starts=list(starts.values()),
                format="fixed_width",
                fast_reader=False,
            )

            self.format_CDMS(response=response)
        
        else:
            raise ValueError("``format'' should be either ``JPL'' or ``CDMS''.")
