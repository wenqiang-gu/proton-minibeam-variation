#!/usr/bin/env python3
"""Generate deterministic TOPAS proton-minibeam inputs from study.toml."""
from __future__ import annotations

import argparse, csv, hashlib, json, math, os, re, shutil, sys, tempfile, tomllib
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
import pydicom
from skimage.draw import polygon


class Error(RuntimeError): pass


@dataclass(frozen=True)
class CT:
    origins: tuple[np.ndarray, ...]
    projections: np.ndarray
    sop_uids: frozenset[str]
    rows: int; cols: int
    row_spacing: float; col_spacing: float; slice_spacing: float
    col_dir: np.ndarray; row_dir: np.ndarray; normal: np.ndarray
    frame_uid: str
    @property
    def size(self):
        return np.array([self.cols*self.col_spacing, self.rows*self.row_spacing,
                         len(self.origins)*self.slice_spacing])


@dataclass(frozen=True)
class Center:
    patient: np.ndarray
    local: np.ndarray
    voxels: int


@dataclass(frozen=True)
class Aperture:
    width: float; ctc: float; shift: float; count: int
    positions: tuple[float, ...]


@dataclass(frozen=True)
class Case:
    case_id: str; width: float; ctc: float; shift: float; angle: float
    aperture: Aperture


def table(c: dict[str, Any], key: str) -> dict[str, Any]:
    value = c.get(key)
    if not isinstance(value, dict): raise Error(f"missing TOML table [{key}]")
    return value


def num(t: dict[str, Any], key: str) -> float:
    v = t.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise Error(f"{key} must be a positive number")
    return float(v)


def integer(t: dict[str, Any], key: str) -> int:
    v = t.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        raise Error(f"{key} must be a positive integer")
    return v


def nums(t: dict[str, Any], key: str, unique=True) -> list[float]:
    v = t.get(key)
    if not isinstance(v, list) or not v or any(isinstance(x, bool) or not isinstance(x, (int,float)) for x in v):
        raise Error(f"{key} must be a non-empty numeric array")
    out = [float(x) for x in v]
    if unique and len(out) != len(set(out)): raise Error(f"{key} contains duplicates")
    return out


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f: c = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e: raise Error(f"cannot read {path}: {e}") from e
    for key in ("study","patient","sweep","aperture","beam","scoring","topas","visualization","profiles"): table(c,key)
    study=table(c,"study")
    for key in ("generated_directory","output_directory"):
        if not isinstance(study.get(key),str) or not study[key].strip(): raise Error(f"study.{key} must be a non-empty path")
    s, a, b, sc, top = table(c,"sweep"), table(c,"aperture"), table(c,"beam"), table(c,"scoring"), table(c,"topas")
    widths, ctcs, shifts = nums(s,"slit_width_mm"), nums(s,"ctc_mm"), nums(s,"shift_fractions")
    nums(s,"angles_deg")
    if any(x <= 0 for x in widths+ctcs) or any(w >= d for w in widths for d in ctcs): raise Error("widths must be positive and smaller than every CTC")
    if any(x < 0 or x >= 1 for x in shifts): raise Error("shifts must satisfy 0 <= shift < 1")
    if num(a,"snout_radius_mm") < num(a,"radius_mm"): raise Error("snout radius must cover aperture")
    for k in ("thickness_mm","slit_height_mm","downstream_surface_distance_mm"): num(a,k)
    integer(a,"max_slits"); integer(b,"spot_count"); num(b,"source_distance_mm")
    if len(nums(sc,"voxel_size_mm",False)) != 3: raise Error("voxel_size_mm needs X, Y, Z")
    integer(top,"threads"); integer(top,"show_history_interval")
    if not isinstance(top.get("physics_modules"),list) or not all(isinstance(x,str) for x in top["physics_modules"]): raise Error("physics_modules must be strings")
    view=table(c,"visualization")
    for key in ("active","include_geometry","include_trajectories","include_axes"):
        if not isinstance(view.get(key),bool): raise Error(f"visualization.{key} must be true or false")
    if not isinstance(view.get("type"),str) or not view["type"]: raise Error("visualization.type must be a non-empty string")
    if not isinstance(view.get("axes_component"),str) or not view["axes_component"]: raise Error("visualization.axes_component must be a non-empty string")
    window=view.get("window_size")
    if not isinstance(window,list) or len(window)!=2 or any(isinstance(x,bool) or not isinstance(x,int) or x<=0 for x in window): raise Error("visualization.window_size must contain two positive integers")
    for key in ("theta_deg","phi_deg"):
        if isinstance(view.get(key),bool) or not isinstance(view.get(key),(int,float)): raise Error(f"visualization.{key} must be numeric")
    for key in ("zoom","axes_size_mm"): num(view,key)
    p=table(c,"patient")
    if not isinstance(p.get("frame_rotation_deg"),list) or len(p["frame_rotation_deg"]) != 3: raise Error("frame_rotation_deg needs three values")
    for name,pf in table(c,"profiles").items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+",name): raise Error(f"unsafe profile name {name!r}")
        if pf.get("history_mode") not in ("uniform","scaled"): raise Error(f"invalid history mode in {name}")
        integer(pf,"chunks"); integer(pf,"histories_per_spot") if pf["history_mode"]=="uniform" else num(pf,"history_scale")
    return c


def read_ct(directory: Path) -> CT:
    datasets=[]
    if not directory.is_dir(): raise Error(f"DICOM directory missing: {directory}")
    for p in sorted(directory.iterdir()):
        if not p.is_file(): continue
        try: d=pydicom.dcmread(p,stop_before_pixels=True)
        except Exception: continue
        if getattr(d,"Modality",None)=="CT": datasets.append((p,d))
    if not datasets: raise Error("no CT DICOM instances found")
    p0,d0=datasets[0]
    required=("Rows","Columns","PixelSpacing","ImageOrientationPatient","ImagePositionPatient","SOPInstanceUID","FrameOfReferenceUID")
    if any(not hasattr(d0,x) for x in required): raise Error(f"CT lacks required geometry tags: {p0}")
    rows,cols=int(d0.Rows),int(d0.Columns); rs,cs=map(float,d0.PixelSpacing)
    orient=np.array(d0.ImageOrientationPatient,float); cd,rd=orient[:3],orient[3:]; normal=np.cross(cd,rd)
    if not np.allclose([np.linalg.norm(cd),np.linalg.norm(rd),np.dot(cd,rd)],[1,1,0],atol=1e-6): raise Error("CT orientation is not orthonormal")
    frame=str(d0.FrameOfReferenceUID); series=str(getattr(d0,"SeriesInstanceUID","")); items=[]
    for p,d in datasets:
        if (int(d.Rows),int(d.Columns))!=(rows,cols) or not np.allclose(d.ImageOrientationPatient,orient,atol=1e-6) or not np.allclose(d.PixelSpacing,[rs,cs],atol=1e-6): raise Error(f"inconsistent CT geometry: {p}")
        if str(d.FrameOfReferenceUID)!=frame or str(getattr(d,"SeriesInstanceUID",""))!=series: raise Error(f"mixed CT series: {p}")
        origin=np.array(d.ImagePositionPatient,float); items.append((float(origin@normal),origin,str(d.SOPInstanceUID)))
    items.sort(key=lambda x:x[0]); projections=np.array([x[0] for x in items]); diffs=np.diff(projections)
    if len(items)>1:
        ss=float(np.median(diffs))
        if np.any(diffs<=1e-6) or not np.allclose(diffs,ss,atol=1e-3): raise Error("CT slice spacing is duplicated or nonuniform")
    else:
        ss=float(getattr(d0,"SliceThickness",0));
        if ss<=0: raise Error("single CT slice needs SliceThickness")
    return CT(tuple(x[1] for x in items),projections,frozenset(x[2] for x in items),rows,cols,rs,cs,ss,cd,rd,normal,frame)


def roi_center(ct: CT, rt_path: Path, roi_name: str) -> Center:
    try: ds=pydicom.dcmread(rt_path)
    except Exception as e: raise Error(f"cannot read RTSTRUCT {rt_path}: {e}") from e
    if getattr(ds,"Modality",None)!="RTSTRUCT": raise Error("configured RTSTRUCT is not RTSTRUCT modality")
    frames={str(x.FrameOfReferenceUID) for x in getattr(ds,"ReferencedFrameOfReferenceSequence",[]) if hasattr(x,"FrameOfReferenceUID")}
    if frames and ct.frame_uid not in frames: raise Error("RTSTRUCT and CT frame of reference differ")
    rois=[x for x in getattr(ds,"StructureSetROISequence",[]) if str(x.ROIName)==roi_name]
    if len(rois)!=1: raise Error(f"expected exactly one ROI named {roi_name!r}; found {len(rois)}")
    number=int(rois[0].ROINumber); groups=[x for x in getattr(ds,"ROIContourSequence",[]) if int(x.ReferencedROINumber)==number]
    if len(groups)!=1 or not getattr(groups[0],"ContourSequence",None): raise Error(f"ROI {roi_name!r} has no unique contours")
    masks={}
    for n,c in enumerate(groups[0].ContourSequence,1):
        if str(getattr(c,"ContourGeometricType","")) not in ("CLOSED_PLANAR","CLOSEDPLANAR_XOR"): raise Error(f"unsupported contour type at {n}")
        values=np.array(getattr(c,"ContourData",[]),float)
        if len(values)<9 or len(values)%3: raise Error(f"malformed contour {n}")
        pts=values.reshape(-1,3); projections=pts@ct.normal
        if np.ptp(projections)>1e-3: raise Error(f"nonplanar contour {n}")
        z=int(np.argmin(abs(ct.projections-projections.mean())))
        if abs(ct.projections[z]-projections.mean())>max(1e-3,ct.slice_spacing/2): raise Error(f"contour {n} is off the CT grid")
        refs={str(x.ReferencedSOPInstanceUID) for x in getattr(c,"ContourImageSequence",[]) if hasattr(x,"ReferencedSOPInstanceUID")}
        if refs and not refs <= ct.sop_uids: raise Error(f"contour {n} references another CT series")
        delta=pts-ct.origins[z]; cc=delta@ct.col_dir/ct.col_spacing; rr=delta@ct.row_dir/ct.row_spacing
        r,cx=polygon(rr,cc,(ct.rows,ct.cols)); cm=np.zeros((ct.rows,ct.cols),bool); cm[r,cx]=True
        masks.setdefault(z,np.zeros_like(cm)); masks[z]^=cm
    count=0; ps=np.zeros(3); ls=np.zeros(3)
    for z,mask in masks.items():
        rr,cc=np.nonzero(mask); n=len(rr)
        count+=n
        ps += (ct.origins[z]+cc[:,None]*ct.col_spacing*ct.col_dir+rr[:,None]*ct.row_spacing*ct.row_dir).sum(axis=0)
        ls += [((cc+.5)*ct.col_spacing).sum(),((rr+.5)*ct.row_spacing).sum(),n*(z+.5)*ct.slice_spacing]
    if not count: raise Error(f"ROI {roi_name!r} rasterized to zero voxels")
    return Center(ps/count,ls/count,count)


VECTOR_RE=re.compile(r"^[diu]v:Tf/Scatterer1/L(?P<n>\d+)/Values\s*=\s*(?P<v>.*?)\s*$",re.M)
def beam_histories(path: Path, spots: int) -> list[int]:
    try: text=path.read_text()
    except OSError as e: raise Error(f"cannot read beam file: {e}") from e
    vectors={int(m.group("n")):m.group("v").split() for m in VECTOR_RE.finditer(text)}
    if set(range(15))-set(vectors): raise Error("beam file lacks one or more L0-L14 vectors")
    for n,tokens in vectors.items():
        values=tokens[1:]
        if len(values)==spots+1 and re.fullmatch(r"[A-Za-z]+",values[-1]): values.pop()
        if int(tokens[0])!=spots or len(values)!=spots: raise Error(f"beam L{n} does not contain {spots} values")
    try: out=[int(x) for x in vectors[4][1:]]
    except ValueError as e: raise Error("beam L4 histories are not integers") from e
    if any(x<=0 for x in out): raise Error("beam histories must be positive")
    return out


def slit_count(width,ctc,shifts,radius,height,maximum):
    for count in range(maximum,0,-1):
        mid=(count-1)/2
        if all((abs((i-mid)*ctc+s*ctc)+width/2)**2+(height/2)**2 <= radius**2+1e-9 for s in shifts for i in range(count)): return count
    raise Error("no slit fits the configured aperture")


def cases(c,profile):
    s,a=table(c,"sweep"),table(c,"aperture"); ws,ds,ss,angles=nums(s,"slit_width_mm"),nums(s,"ctc_mm"),nums(s,"shift_fractions"),nums(s,"angles_deg")
    counts={(w,d):slit_count(w,d,ss,num(a,"radius_mm"),num(a,"slit_height_mm"),integer(a,"max_slits")) for w,d in product(ws,ds)}; out=[]
    for w,d,shift,angle in product(ws,ds,ss,angles):
        count=counts[w,d]; mid=(count-1)/2; ap=Aperture(w,d,shift,count,tuple((i-mid)*d+shift*d for i in range(count)))
        ident=f"{profile}_sw{round(w*100):03d}_ctc{round(d*100):03d}_shift{round(shift*100):03d}_angle{round(angle)%360:03d}"
        out.append(Case(ident,w,d,shift,angle,ap))
    if len(out)!=len({x.case_id for x in out}): raise Error("case IDs collide at encoded precision")
    return out


def profile_data(c,name,production):
    pf=table(c,"profiles").get(name)
    if not isinstance(pf,dict): raise Error(f"unknown profile {name!r}")
    chunks=integer(pf,"chunks")
    if pf["history_mode"]=="uniform": histories=[integer(pf,"histories_per_spot")]*len(production)
    else:
        scale=Decimal(str(num(pf,"history_scale"))); histories=[max(1,int((Decimal(x)*scale).quantize(Decimal(1),rounding=ROUND_HALF_UP))) for x in production]
    return histories,chunks


def split_histories(histories,chunks):
    out=[[0]*len(histories) for _ in range(chunks)]
    for spot,value in enumerate(histories):
        q,r=divmod(value,chunks)
        for j in range(chunks): out[j][spot]=q
        for j in range(r): out[(spot+j)%chunks][spot]+=1
    assert [sum(x[i] for x in out) for i in range(len(histories))]==histories
    return out


def fmt(x): return f"{x:.10g}"
def seed(profile,case,chunk): return int.from_bytes(hashlib.sha256(f"minibeam:{profile}:{case}:{chunk}".encode()).digest()[:4],"big")%2147483646+1
def write(path,text): path.parent.mkdir(parents=True,exist_ok=True); path.write_text(text)
def ap_name(ap): return f"sw{round(ap.width*100):03d}_ctc{round(ap.ctc*100):03d}_shift{round(ap.shift*100):03d}.txt"


def render_patient(c,ct,center):
    p,sc=table(c,"patient"),table(c,"scoring"); trans=ct.size/2-center.local; rot=p["frame_rotation_deg"]; vox=nums(sc,"voxel_size_mm",False)
    return f'''# Generated; do not edit. PTV patient XYZ = {" ".join(fmt(x) for x in center.patient)} mm
s:Ge/PatientFrame/Parent = "World"
s:Ge/PatientFrame/Type = "Group"
d:Ge/PatientFrame/RotX = {fmt(rot[0])} deg
d:Ge/PatientFrame/RotY = {fmt(rot[1])} deg
d:Ge/PatientFrame/RotZ = {fmt(rot[2])} deg
s:Ge/Patient/Parent = "PatientFrame"
s:Ge/Patient/Type = "TsDicomPatient"
s:Ge/Patient/Material = "G4_WATER"
s:Ge/Patient/DicomDirectory = "{p["dicom_directory"]}"
sv:Ge/Patient/DicomModalityTags = 1 "CT"
d:Ge/Patient/TransX = {fmt(trans[0])} mm
d:Ge/Patient/TransY = {fmt(trans[1])} mm
d:Ge/Patient/TransZ = {fmt(trans[2])} mm
b:Ge/Patient/SchneiderUseVariableDensityMaterials = "True"
includeFile = reference/HUtoMaterialSchneider.txt
dv:Ge/Patient/CloneRTDoseGridSize = 3 {fmt(vox[0])} {fmt(vox[1])} {fmt(vox[2])} mm
'''


def render_source(c):
    beam=table(c,"beam"); distance=num(beam,"source_distance_mm")
    return f'''# Generated beam-1 emittance source; do not edit.
includeFile = {beam["time_feature_file"]}

s:Ge/BeamPosition2/Parent = "Gantry"
s:Ge/BeamPosition2/Type = "Group"
d:Ge/BeamPosition2/TransX = -1.0 * Tf/Scatterer1/L5/Value mm
d:Ge/BeamPosition2/TransY = Tf/Scatterer1/L6/Value mm
d:Ge/BeamPosition2/TransZ = -{fmt(distance)} mm
d:Ge/BeamPosition2/RotX = Tf/Scatterer1/L7/Value deg
d:Ge/BeamPosition2/RotY = Tf/Scatterer1/L8/Value deg
s:So/ProtonSource/Type = "emittance"
s:So/ProtonSource/Component = "BeamPosition2"
s:So/ProtonSource/BeamParticle = "proton"
s:So/ProtonSource/Distribution = "BiGaussian"
d:So/ProtonSource/BeamEnergy = Tf/Scatterer1/L2/Value MeV
u:So/ProtonSource/BeamEnergySpread = Tf/Scatterer1/L3/Value
d:So/ProtonSource/SigmaX = Tf/Scatterer1/L9/Value mm
u:So/ProtonSource/SigmaXprime = Tf/Scatterer1/L10/Value
u:So/ProtonSource/CorrelationX = Tf/Scatterer1/L11/Value
d:So/ProtonSource/SigmaY = Tf/Scatterer1/L12/Value mm
u:So/ProtonSource/SigmaYprime = Tf/Scatterer1/L13/Value
u:So/ProtonSource/CorrelationY = Tf/Scatterer1/L14/Value
i:So/ProtonSource/NumberOfHistoriesInRun = Tf/Scatterer1/L4/Value
'''


def render_aperture(c,ap):
    a=table(c,"aperture"); radius=num(a,"radius_mm"); snout=num(a,"snout_radius_mm"); half=num(a,"thickness_mm")/2; hh=num(a,"slit_height_mm")/2
    z=-(num(a,"downstream_surface_distance_mm")+half)
    lines=["# Generated fixed-radius aperture; do not edit.",f"# width={fmt(ap.width)} ctc={fmt(ap.ctc)} shift={fmt(ap.shift)} slits={ap.count}",
      's:Ge/Snout/Parent = "Gantry"','s:Ge/Snout/Type = "TsCylinder"','s:Ge/Snout/Material = "Air"',
      'd:Ge/Snout/RMin = 0 mm',f'd:Ge/Snout/RMax = {fmt(snout)} mm',f'd:Ge/Snout/HL = {fmt(half)} mm','d:Ge/Snout/SPhi = 0 deg','d:Ge/Snout/DPhi = 360 deg',f'd:Ge/Snout/TransZ = {fmt(z)} mm',
      's:Ge/Aperture/Parent = "Snout"','s:Ge/Aperture/Type = "TsCylinder"','s:Ge/Aperture/Material = "Brass"','d:Ge/Aperture/RMin = 0 mm',f'd:Ge/Aperture/RMax = {fmt(radius)} mm',f'd:Ge/Aperture/HL = {fmt(half)} mm','d:Ge/Aperture/SPhi = 0 deg','d:Ge/Aperture/DPhi = 360 deg']
    for n,x in enumerate(ap.positions,1):
        p=f"Ge/Aperture/Slit{n:02d}"; hw=ap.width/2
        lines += ["",f's:{p}/Parent = "Aperture"',f's:{p}/Type = "G4GTrap"',f's:{p}/Material = "Air"',f'd:{p}/TransX = {fmt(x)} mm',
          f'd:{p}/HLX1 = {fmt(hw)} mm',f'd:{p}/HLY1 = {fmt(hh)} mm',f'd:{p}/HLZ = {fmt(half)} mm',f'd:{p}/HLX2 = {fmt(hw)} mm',f'd:{p}/HLY2 = {fmt(hh)} mm',f'd:{p}/HLX3 = {fmt(hw)} mm',f'd:{p}/HLX4 = {fmt(hw)} mm',f'd:{p}/Theta = 0 deg',f'd:{p}/Phi = 0 deg',f'd:{p}/Alp1 = 0 deg',f'd:{p}/Alp2 = 0 deg']
    return "\n".join(lines)+"\n"


def tf_bool(value): return '"True"' if value else '"False"'


def render_visualization(c):
    view=table(c,"visualization")
    if not view["active"]: return 'b:Gr/ViewA/Active = "False"\n'
    width,height=view["window_size"]
    return f'''s:Gr/ViewA/Type = "{view["type"]}"
b:Gr/ViewA/Active = "True"
i:Gr/ViewA/WindowSizeX = {width}
i:Gr/ViewA/WindowSizeY = {height}
d:Gr/ViewA/Theta = {fmt(view["theta_deg"])} deg
d:Gr/ViewA/Phi = {fmt(view["phi_deg"])} deg
u:Gr/ViewA/Zoom = {fmt(view["zoom"])}
b:Gr/ViewA/IncludeGeometry = {tf_bool(view["include_geometry"])}
b:Gr/ViewA/IncludeTrajectories = {tf_bool(view["include_trajectories"])}
b:Gr/ViewA/IncludeAxes = {tf_bool(view["include_axes"])}
s:Gr/ViewA/AxesComponent = "{view["axes_component"]}"
d:Gr/ViewA/AxesSize = {fmt(view["axes_size_mm"])} mm
'''


def render_field(c,case,profile):
    study,beam,top,sc=table(c,"study"),table(c,"beam"),table(c,"topas"),table(c,"scoring"); base=Path(study["generated_directory"])/profile
    modules=" ".join(f'"{x}"' for x in top["physics_modules"]); spots=integer(beam,"spot_count")
    return f'''# Generated field {case.case_id}; do not edit.
s:Ge/World/Material = "Air"
d:Ge/World/HLX = 2 m
d:Ge/World/HLY = 2 m
d:Ge/World/HLZ = 2 m
sv:Ph/Default/Modules = {len(top["physics_modules"])} {modules}
i:Ts/ShowHistoryCountAtInterval = {integer(top,"show_history_interval")}
i:Gr/ShowOnlyOutlineIfVoxelCountExceeds = 2000000
d:Ge/FieldAngle = {fmt(case.angle)} deg
d:Tf/TimelineEnd = {spots} ms
i:Tf/NumberOfSequentialTimes = {spots}
s:Ge/Gantry/Parent = "World"
s:Ge/Gantry/Type = "Group"
d:Ge/Gantry/RotY = -1.0 * Ge/FieldAngle deg
includeFile = {(base/'common/patient.txt').as_posix()}
includeFile = {(base/'apertures'/ap_name(case.aperture)).as_posix()}
includeFile = {(base/'common/source.txt').as_posix()}
s:Sc/PatientDose/Quantity = "DoseToMedium"
s:Sc/PatientDose/Component = "Patient/RTDoseGrid"
s:Sc/PatientDose/OutputType = "{sc.get("output_type","Binary")}"
s:Sc/PatientDose/IfOutputFileAlreadyExists = "Overwrite"
b:Sc/PatientDose/OutputToConsole = "False"
sv:Sc/PatientDose/Report = 1 "Sum"
{render_visualization(c)}
'''


def render_task(c,case,profile,chunk,chunks,histories):
    study=table(c,"study"); spots=integer(table(c,"beam"),"spot_count"); base=Path(study["generated_directory"])/profile
    suffix=f"chunk_{chunk:03d}_of_{chunks:03d}"; output=Path(study["output_directory"])/profile/case.case_id/f"Dose_{suffix}"; sd=seed(profile,case.case_id,chunk)
    text=f'''# Generated runnable task; do not edit.
includeFile = {(base/'fields'/f'{case.case_id}.txt').as_posix()}
i:Ts/NumberOfThreads = {integer(table(c,"topas"),"threads")}
i:Ts/Seed = {sd}
iv:Tf/Scatterer1/L4/Values = {spots} {" ".join(map(str,histories))}
s:Sc/PatientDose/OutputFile = "{output.as_posix()}"
s:Sc/PatientDose/SeriesDescription = "{case.case_id} {suffix}"
'''
    return text,output.as_posix(),sd


def build_tree(dest,root,c,profile,ct,center,production):
    histories,chunks=profile_data(c,profile,production); chunks_data=split_histories(histories,chunks); all_cases=cases(c,profile)
    write(dest/'common/patient.txt',render_patient(c,ct,center)); write(dest/'common/source.txt',render_source(c))
    apertures={ap_name(x.aperture):x.aperture for x in all_cases}
    for name,ap in sorted(apertures.items()): write(dest/'apertures'/name,render_aperture(c,ap))
    for case in all_cases: write(dest/'fields'/f'{case.case_id}.txt',render_field(c,case,profile))
    records=[]; paths=[]; study=table(c,"study")
    for case in all_cases:
        for j,h in enumerate(chunks_data,1):
            name=f"{case.case_id}_chunk_{j:03d}_of_{chunks:03d}.txt"; rel=(Path(study["generated_directory"])/profile/'tasks'/name).as_posix()
            text,output,sd=render_task(c,case,profile,j,chunks,h); write(dest/'tasks'/name,text); paths.append(rel)
            records.append(dict(case_id=case.case_id,profile=profile,slit_width_mm=case.width,ctc_mm=case.ctc,shift_fraction=case.shift,shift_mm=case.shift*case.ctc,angle_deg=case.angle,slit_count=case.aperture.count,chunk=j,chunks=chunks,chunk_histories=sum(h),seed=sd,input_path=rel,output_path=output))
    write(dest/'inputs.txt',"\n".join(paths)+"\n"); write(dest/'manifest.json',json.dumps(records,indent=2)+"\n")
    (dest/'manifest.csv').parent.mkdir(parents=True,exist_ok=True)
    with (dest/'manifest.csv').open('w',newline='') as f: w=csv.DictWriter(f,fieldnames=list(records[0])); w.writeheader(); w.writerows(records)
    summary=dict(profile=profile,case_count=len(all_cases),task_count=len(records),aperture_count=len(apertures),spot_count=len(histories),histories_per_case=sum(histories),chunks_per_case=chunks,ct_shape=[ct.cols,ct.rows,len(ct.origins)],ct_spacing_mm=[ct.col_spacing,ct.row_spacing,ct.slice_spacing],ptv_voxel_count=center.voxels,isocenter_patient_xyz_mm=center.patient.tolist(),isocenter_local_xyz_mm=center.local.tolist())
    write(dest/'summary.json',json.dumps(summary,indent=2)+"\n"); return len(all_cases),len(records)


def differences(expected,actual):
    ef={x.relative_to(expected) for x in expected.rglob('*') if x.is_file()}; af={x.relative_to(actual) for x in actual.rglob('*') if x.is_file()} if actual.is_dir() else set()
    out=[f"missing: {x}" for x in sorted(ef-af)]+[f"unexpected: {x}" for x in sorted(af-ef)]
    out += [f"stale: {x}" for x in sorted(ef&af) if (expected/x).read_bytes()!=(actual/x).read_bytes()]
    return out


def case_output_directories(root,c,profile):
    root=root.resolve(); output_root=(root/table(c,"study")["output_directory"]).resolve()
    try: output_root.relative_to(root)
    except ValueError as e: raise Error("output_directory must be inside the project root") from e
    if output_root==root: raise Error("output_directory cannot be the project root")
    return [output_root/profile/case.case_id for case in cases(c,profile)]


def ensure_output_directories(root,c,profile):
    directories=case_output_directories(root,c,profile)
    for directory in directories: directory.mkdir(parents=True,exist_ok=True)
    return directories


def execute(config_path,profile,check=False,force=False,clean=False):
    config_path=config_path.resolve(); root=config_path.parent; c=load_config(config_path)
    if profile not in table(c,"profiles"): raise Error(f"unknown profile {profile!r}")
    generated=(root/table(c,"study")["generated_directory"]).resolve()
    try: generated.relative_to(root)
    except ValueError as e: raise Error("generated_directory must be inside the project root") from e
    if generated==root: raise Error("generated_directory cannot be the project root")
    target=generated/profile
    if clean:
        if target.exists(): shutil.rmtree(target); print(f"Removed {target}")
        else: print(f"Nothing to remove: {target}")
        return 0
    p=table(c,"patient"); ct=read_ct((root/p["dicom_directory"]).resolve()); center=roi_center(ct,(root/p["rtstruct"]).resolve(),str(p["roi_name"]))
    b=table(c,"beam"); production=beam_histories((root/b["time_feature_file"]).resolve(),integer(b,"spot_count"))
    generated.mkdir(parents=True,exist_ok=True); temporary: Path|None=Path(tempfile.mkdtemp(prefix=f'.{profile}.',dir=generated))
    try:
        count,tasks=build_tree(temporary,root,c,profile,ct,center,production); diff=differences(temporary,target)
        if check:
            missing_outputs=[path for path in case_output_directories(root,c,profile) if not path.is_dir()]
            if missing_outputs: diff.append(f"missing output directories: {len(missing_outputs)}")
            if diff: raise Error("generated profile is not current:\n  "+"\n  ".join(diff[:20]))
            print(f"Validated {count} fields and {tasks} tasks in {target}"); return 0
        if target.exists() and diff and not force: raise Error(f"{target} is stale; rerun with --force")
        if target.exists() and not diff:
            ensure_output_directories(root,c,profile); print(f"Already current: {target}"); return 0
        if target.exists(): shutil.rmtree(target)
        os.replace(temporary,target); temporary=None; ensure_output_directories(root,c,profile)
        print(f"Generated {count} independent fields and {tasks} runnable tasks in {target}")
        print("PTV centroid (patient XYZ mm): "+", ".join(f"{x:.5f}" for x in center.patient)); return 0
    finally:
        if temporary is not None and temporary.is_dir(): shutil.rmtree(temporary)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--config',type=Path,default=Path('study.toml')); parser.add_argument('--profile',default='smoke')
    action=parser.add_mutually_exclusive_group(); action.add_argument('--check',action='store_true'); action.add_argument('--clean',action='store_true'); parser.add_argument('--force',action='store_true'); a=parser.parse_args(argv)
    if a.force and (a.check or a.clean): parser.error('--force cannot be combined with --check or --clean')
    try: return execute(a.config,a.profile,a.check,a.force,a.clean)
    except Error as e: print(f"error: {e}",file=sys.stderr); return 2


if __name__=='__main__': raise SystemExit(main())
