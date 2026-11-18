"""Exact native-cell plane sections with preserved cell associations."""

from fractions import Fraction
import numpy as np

def _hold(reason):
    raise ValueError("无法生成所请求的网格截面；原因：NATIVE_SNAPSHOT_HOLD:" + reason)

def axis_section(mesh,axis,position,*,cancelled=lambda:False,remaining_seconds=lambda:float('inf')):
    """Shared exact plane operation; caller retains its own budgets/authority."""
    def checkpoint():
        if cancelled() or remaining_seconds()<=0:raise InterruptedError('CANCELLED_OR_TIME_BUDGET')
    checkpoint()
    if axis not in ('x','y','z') or not np.isfinite(position):_hold('SECTION_REQUEST')
    index='xyz'.index(axis);normal=[0,0,0];normal[index]=1
    if np.any(mesh.celltypes==42):
        if not np.all(mesh.celltypes==42):_hold('MIXED_POLYHEDRON_SECTION_UNSUPPORTED')
        return _polyhedron_section(mesh,index,float(position),checkpoint=checkpoint)
    origin=[(mesh.bounds[2*i]+mesh.bounds[2*i+1])/2 for i in range(3)];origin[index]=float(position)
    import vtk
    import pyvista as pv
    plane=vtk.vtkPlane();plane.SetNormal(normal);plane.SetOrigin(origin)
    cutter=vtk.vtkCutter();cutter.SetInputData(mesh);cutter.SetCutFunction(plane)
    # Default cutter output can round even double input points to float32.
    cutter.SetOutputPointsPrecision(vtk.vtkAlgorithm.DOUBLE_PRECISION)
    cutter.GenerateTrianglesOff();checkpoint();cutter.Update();checkpoint()
    result=pv.wrap(cutter.GetOutput()).triangulate();checkpoint()
    return result

def _native_ring_triangles(xy,checkpoint):
    """Triangulate one original simple ring without merging close vertices.

    The floating determinant is only a fast sign proof. Uncertain signs use
    exact rationals of the input doubles, never an epsilon geometry decision.
    """
    n=len(xy)
    if n<3 or len(set(xy))!=n:_hold('DEGENERATE_NATIVE_SECTION')
    exact=[]
    for i,p in enumerate(xy):
        if i%64==0:checkpoint()
        exact.append(tuple(Fraction(v) for v in p))
    area=Fraction(0)
    for i,(a,b) in enumerate(zip(exact,exact[1:]+exact[:1])):
        if i%64==0:checkpoint()
        area+=a[0]*b[1]-a[1]*b[0]
    if not area:_hold('DEGENERATE_NATIVE_SECTION')
    winding=1 if area>0 else -1
    def turn(i,j,k):
        a,b,c=xy[i],xy[j],xy[k]
        left=(b[0]-a[0])*(c[1]-a[1]);right=(b[1]-a[1])*(c[0]-a[0]);det=left-right
        # Conservative roundoff bound for the two products and subtraction;
        # overflow/underflow and near cancellation fall back to exact inputs.
        scale=abs(left)+abs(right)
        if scale>=np.finfo(np.float64).tiny/np.finfo(np.float64).eps and abs(det)>4*np.finfo(np.float64).eps*scale:
            return 1 if det>0 else -1
        a,b,c=exact[i],exact[j],exact[k]
        det=(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        return (det>0)-(det<0)
    def on_segment(i,j,k):
        return all(min(xy[i][d],xy[j][d])<=xy[k][d]<=max(xy[i][d],xy[j][d]) for d in (0,1))
    # A connected contour graph alone does not prove a simple geometric ring.
    for i in range(n):
        checkpoint();b=(i+1)%n
        for j in range(i+1,n):
            if j%64==0:checkpoint()
            d=(j+1)%n
            if b==j or d==i:continue
            abj,abd,jdi,jdb=turn(i,b,j),turn(i,b,d),turn(j,d,i),turn(j,d,b)
            if (abj*abd<0 and jdi*jdb<0 or abj==0 and on_segment(i,b,j)
                    or abd==0 and on_segment(i,b,d) or jdi==0 and on_segment(j,d,i)
                    or jdb==0 and on_segment(j,d,b)):_hold('SELF_TOUCHING_NATIVE_SECTION')
    alive=list(range(n));triangles=[]
    while len(alive)>3:
        checkpoint()
        for step,current in enumerate(alive):
            if step%64==0:checkpoint()
            previous,following=alive[step-1],alive[(step+1)%len(alive)]
            if winding*turn(previous,current,following)<=0:continue
            blocked=False
            for candidate,other in enumerate(alive):
                if candidate%64==0:checkpoint()
                if other in (previous,current,following):continue
                if (winding*turn(previous,current,other)>=0 and winding*turn(current,following,other)>=0
                        and winding*turn(following,previous,other)>=0):blocked=True;break
            if not blocked:
                triangles.append((previous,current,following));alive.pop(step);break
        else:_hold('NATIVE_SECTION_TRIANGULATION_UNPROVEN')
    if winding*turn(*alive)<=0:_hold('DEGENERATE_NATIVE_SECTION')
    triangles.append(tuple(alive));checkpoint()
    return triangles

def _triangulate_native_polygons(poly,axis,checkpoint):
    """Keep every original ring edge and its native cell/field association."""
    import pyvista as pv
    points=np.asarray(poly.points,dtype=np.float64);faces=np.asarray(poly.faces);offset=0;triangles=[];parents=[]
    dims=[d for d in range(3) if d!=axis]
    for parent in range(poly.n_cells):
        checkpoint();n=int(faces[offset]);vertices=faces[offset+1:offset+1+n];offset+=n+1
        xy=[tuple(float(points[v,d]) for d in dims) for v in vertices]
        for triangle in _native_ring_triangles(xy,checkpoint):
            triangles.extend((3,*(int(vertices[i]) for i in triangle)));parents.append(parent)
    checkpoint();result=pv.PolyData(points,np.asarray(triangles,dtype=np.int64),deep=True)
    for name,values in poly.cell_data.items():result.cell_data[name]=np.asarray(values)[parents]
    for name,values in poly.field_data.items():result.field_data[name]=values
    checkpoint();return result

def _polyhedron_section(mesh,axis,position,*,checkpoint):
    """Intersect original face edges, without vtkPolyhedron's vertex perturbation.

    Coincident faces use the negative-side trace, independent of chunk bounds.
    Unsupported non-manifold/touching contour graphs reject the whole request.
    """
    import pyvista as pv
    points=np.asarray(mesh.points,dtype=np.float64);out=[];polygons=[];parents=[]
    def emit(coords,parent):
        if len(coords)<3:_hold('DEGENERATE_NATIVE_SECTION')
        start=len(out);out.extend(coords);polygons.extend((len(coords),*range(start,start+len(coords))));parents.append(parent)
    for cell_index in range(mesh.n_cells):
        if cell_index%64==0:checkpoint()
        cell=mesh.GetCell(cell_index)
        if cell.GetNumberOfFaces()<4:_hold('POLYHEDRON_FACE_TOPOLOGY_MISSING')
        native_ids=[cell.GetPointId(j) for j in range(cell.GetNumberOfPoints())]
        distance=points[native_ids,axis]-position
        # Positive-side tangent points/edges have zero section area. Exact
        # coincident faces are owned by cells on the negative side only.
        if distance.min()>=0 or distance.max()<0:continue
        faces=[]
        for f in range(cell.GetNumberOfFaces()):
            if f%64==0:checkpoint()
            face=cell.GetFace(f);faces.append([face.GetPointId(j) for j in range(face.GetNumberOfPoints())])
        coplanar=[face for face in faces if np.all(points[face,axis]==position)]
        if coplanar:
            if distance.min()<0 and distance.max()==0:
                for face in coplanar:emit(points[face].copy(),cell_index)
            elif distance.min()<0 and distance.max()>0:_hold('NONCONVEX_COPLANAR_SECTION')
            continue
        if distance.max()==0:continue  # Negative-side tangent point/edge only.
        nodes={};edges=set()
        for face in faces:
            checkpoint()
            keys=[]
            for a,b in zip(face,face[1:]+face[:1]):
                da,db=points[a,axis]-position,points[b,axis]-position
                if da==0:
                    key=('v',a);nodes[key]=points[a].copy();keys.append(key)
                if da*db<0:
                    lo,hi=sorted((a,b));key=('e',lo,hi)
                    fraction=(position-points[lo,axis])/(points[hi,axis]-points[lo,axis])
                    xyz=points[lo]+fraction*(points[hi]-points[lo]);xyz[axis]=position
                    nodes[key]=xyz;keys.append(key)
            keys=list(dict.fromkeys(keys))
            if len(keys)>2:_hold('COMPLEX_FACE_PLANE_INTERSECTION')
            if len(keys)==2:edges.add(tuple(sorted(keys)))
        if not edges:continue
        graph={}
        for a,b in edges:graph.setdefault(a,set()).add(b);graph.setdefault(b,set()).add(a)
        if any(len(neighbours)!=2 for neighbours in graph.values()):_hold('NONMANIFOLD_SECTION_GRAPH:cell='+str(cell_index))
        unused=set(graph);loops=[]
        while unused:
            first=min(unused);loop=[];previous=None;current=first
            while True:
                if current in loop:_hold('SELF_TOUCHING_SECTION_GRAPH')
                loop.append(current);unused.discard(current)
                following=min(graph[current]-({previous} if previous is not None else set()))
                previous,current=current,following
                if current==first:break
            loops.append(loop)
        # Nested loops require a holes contract; never fill a hole silently.
        if len(loops)!=1:_hold('MULTIPLE_NATIVE_SECTION_LOOPS')
        emit(np.array([nodes[key] for key in loops[0]],dtype=np.float64),cell_index)
    checkpoint()
    if not parents:return pv.PolyData()
    result=pv.PolyData(np.asarray(out,dtype=np.float64),np.asarray(polygons,dtype=np.int64))
    for name,array in mesh.cell_data.items():result.cell_data[name]=np.asarray(array)[parents]
    for name,array in mesh.field_data.items():result.field_data[name]=array
    result.field_data['coplanar_rule']=['negative_side_trace_at_exact_native_coplanar_faces']
    checkpoint();triangles=_triangulate_native_polygons(result,axis,checkpoint);checkpoint()
    return triangles
