import os, struct, subprocess, sys
from decimal import Decimal

from migen.fhdl.std import *
from migen.fhdl.specials import SynthesisDirective
from migen.genlib.cdc import *

from mibuild.generic_platform import *
from mibuild.crg import SimpleCRG
from mibuild import tools

def _add_period_constraint(platform, clk, period):
	if period is not None:
		platform.add_platform_command("""NET "{clk}" TNM_NET = "GRPclk";
TIMESPEC "TSclk" = PERIOD "GRPclk" """+str(period)+""" ns HIGH 50%;""", clk=clk)

class CRG_SE(SimpleCRG):
	def __init__(self, platform, clk_name, rst_name, period=None, rst_invert=False):
		SimpleCRG.__init__(self, platform, clk_name, rst_name, rst_invert)
		_add_period_constraint(platform, self._clk, period)

class CRG_DS(Module):
	def __init__(self, platform, clk_name, rst_name, period=None, rst_invert=False):
		reset_less = rst_name is None
		self.clock_domains.cd_sys = ClockDomain(reset_less=reset_less)
		self._clk = platform.request(clk_name)
		_add_period_constraint(platform, self._clk.p, period)
		self.specials += Instance("IBUFGDS",
			Instance.Input("I", self._clk.p),
			Instance.Input("IB", self._clk.n),
			Instance.Output("O", self.cd_sys.clk)
		)
		if not reset_less:
			if rst_invert:
				self.comb += self.cd_sys.rst.eq(~platform.request(rst_name))
			else:
				self.comb += self.cd_sys.rst.eq(platform.request(rst_name))

def _format_constraint(c):
	if isinstance(c, Pins):
		return "LOC=" + c.identifiers[0]
	elif isinstance(c, IOStandard):
		return "IOSTANDARD=" + c.name
	elif isinstance(c, Drive):
		return "DRIVE=" + str(c.strength)
	elif isinstance(c, Misc):
		return c.misc

def _format_ucf(signame, pin, others, resname):
	fmt_c = []
	for c in [Pins(pin)] + others:
		fc = _format_constraint(c)
		if fc is not None:
			fmt_c.append(fc)
	fmt_r = resname[0] + ":" + str(resname[1])
	if resname[2] is not None:
		fmt_r += "." + resname[2]
	return "NET \"" + signame + "\" " + " | ".join(fmt_c) + "; # " + fmt_r + "\n"

def _build_ucf(named_sc, named_pc):
	r = ""
	for sig, pins, others, resname in named_sc:
		if len(pins) > 1:
			for i, p in enumerate(pins):
				r += _format_ucf(sig + "(" + str(i) + ")", p, others, resname)
		else:
			r += _format_ucf(sig, pins[0], others, resname)
	if named_pc:
		r += "\n" + "\n\n".join(named_pc)
	return r

def _build_files(device, sources, named_sc, named_pc, build_name):
	tools.write_to_file(build_name + ".ucf", _build_ucf(named_sc, named_pc))

	prj_contents = ""
	for filename, language in sources:
		prj_contents += language + " work " + filename + "\n"
	tools.write_to_file(build_name + ".prj", prj_contents)

	xst_contents = """run
-ifn %s.prj
-top top
-ifmt MIXED
-opt_mode SPEED
-reduce_control_sets auto
-register_balancing yes
-ofn %s.ngc
-p %s""" % (build_name, build_name, device)
	tools.write_to_file(build_name + ".xst", xst_contents)

def _is_valid_version(path, v):
	try: 
		Decimal(v)
		return os.path.isdir(os.path.join(path, v))
	except:
		return False

def _run_ise(build_name, ise_path, source):
	if sys.platform == "win32" or sys.platform == "cygwin":
		source = False
	build_script_contents = "# Autogenerated by mibuild\nset -e\n"
	if source:
		vers = [ver for ver in os.listdir(ise_path) if _is_valid_version(ise_path, ver)]
		tools_version = max(vers)
		bits = struct.calcsize("P")*8
		xilinx_settings_file = '%s/%s/ISE_DS/settings%d.sh' % (ise_path, tools_version, bits) 
		build_script_contents += "source " + xilinx_settings_file + "\n"

	build_script_contents += """
xst -ifn {build_name}.xst
ngdbuild -uc {build_name}.ucf {build_name}.ngc {build_name}.ngd
map -ol high -w -o {build_name}_map.ncd {build_name}.ngd {build_name}.pcf
par -ol high -w {build_name}_map.ncd {build_name}.ncd {build_name}.pcf
bitgen -g LCK_cycle:6 -g Binary:Yes -w {build_name}.ncd {build_name}.bit
""".format(build_name=build_name)
	build_script_file = "build_" + build_name + ".sh"
	tools.write_to_file(build_script_file, build_script_contents)

	r = subprocess.call(["bash", build_script_file])
	if r != 0:
		raise OSError("Subprocess failed")

class XilinxNoRetimingImpl(Module):
	def __init__(self, reg):
		self.specials += SynthesisDirective("attribute register_balancing of {r} is no", r=reg)

class XilinxNoRetiming:
	@staticmethod
	def lower(dr):
		return XilinxNoRetimingImpl(dr.reg)

class XilinxMultiRegImpl(MultiRegImpl):
	def __init__(self, *args, **kwargs):
		MultiRegImpl.__init__(self, *args, **kwargs)
		self.specials += [SynthesisDirective("attribute shreg_extract of {r} is no", r=r)
			for r in self.regs]

class XilinxMultiReg:
	@staticmethod
	def lower(dr):
		return XilinxMultiRegImpl(dr.i, dr.o, dr.odomain, dr.n)

class XilinxISEPlatform(GenericPlatform):
	def get_verilog(self, *args, special_overrides=dict(), **kwargs):
		so = {
			NoRetiming: XilinxNoRetiming,
			MultiReg:   XilinxMultiReg
		}
		so.update(special_overrides)
		return GenericPlatform.get_verilog(self, *args, special_overrides=so, **kwargs)

	def build(self, fragment, build_dir="build", build_name="top",
			ise_path="/opt/Xilinx", source=True, run=True):
		tools.mkdir_noerror(build_dir)
		os.chdir(build_dir)

		v_src, named_sc, named_pc = self.get_verilog(fragment)
		v_file = build_name + ".v"
		tools.write_to_file(v_file, v_src)
		sources = self.sources + [(v_file, "verilog")]
		_build_files(self.device, sources, named_sc, named_pc, build_name)
		if run:
			_run_ise(build_name, ise_path, source)
		
		os.chdir("..")

	def build_arg_ns(self, ns, *args, **kwargs):
		for n in ["build_dir", "build_name", "ise_path"]:
			attr = getattr(ns, n)
			if attr is not None:
				kwargs[n] = attr
		if ns.no_source:
			kwargs["source"] = False
		if ns.no_run:
			kwargs["run"] = False
		self.build(*args, **kwargs)

	def add_arguments(self, parser):
		parser.add_argument("--build-dir", default=None, help="Set the directory in which to generate files and run ISE")
		parser.add_argument("--build-name", default=None, help="Base name for the generated files")
		parser.add_argument("--ise-path", default=None, help="ISE installation path (without version directory)")
		parser.add_argument("--no-source", action="store_true", help="Do not source ISE settings file")
		parser.add_argument("--no-run", action="store_true", help="Only generate files, do not run ISE")
