import time

import enoki as ek
import mitsuba

mitsuba.set_variant('gpu_autodiff_rgb')

from mitsuba.core import Thread, xml, UInt32, Float, Vector2f, Vector3f, Transform4f, ScalarTransform4f
from mitsuba.render import SurfaceInteraction3f
from mitsuba.python.util import traverse
from mitsuba.python.autodiff import render, write_bitmap, Adam

from mylib.util import ravel, unravel


def optimisation(args, scene, params):
    positions_buf = params['grid_mesh.vertex_positions_buf']
    positions_initial = ravel(positions_buf)
    normals_initial = ravel(params['grid_mesh.vertex_normals_buf'])
    vertex_count = ek.slices(positions_initial)

    # Create a texture with the reference displacement map
    disp_tex = xml.load_dict({
        "type": "bitmap",
        "filename": "mitsuba_coin.jpg",
        "to_uv": ScalarTransform4f.scale([1, -1, 1])  # texture is upside-down
    }).expand()[0]

    # Create a fake surface interaction with an entry per vertex on the mesh
    mesh_si = SurfaceInteraction3f.zero(vertex_count)
    mesh_si.uv = ravel(params['grid_mesh.vertex_texcoords_buf'], dim=2)

    # Evaluate the displacement map for the entire mesh
    disp_tex_data_ref = disp_tex.eval_1(mesh_si)

    # Apply displacement to mesh vertex positions and update scene (e.g. OptiX BVH)
    def apply_displacement(amplitude=0.05):
        new_positions = disp_tex.eval_1(mesh_si) * normals_initial * amplitude + positions_initial
        unravel(new_positions, params['grid_mesh.vertex_positions_buf'])
        params.set_dirty('grid_mesh.vertex_positions_buf')
        params.update()

    # Apply displacement before generating reference image
    apply_displacement()
    # Render a reference image (no derivatives used yet)
    image_ref = render(scene, spp=3)
    crop_size = scene.sensors()[0].film().crop_size()
    write_bitmap(args.out + 'out_ref.exr', image_ref, crop_size)
    print("Write " + args.out + "out_ref.exr")

    # Reset texture data to a constant
    disp_tex_params = traverse(disp_tex)
    disp_tex_params.keep(['data'])

    # Reset
    disp_tex_params['data'] = ek.full(Float, 0.25, len(disp_tex_params['data']))
    disp_tex_params.update()

    # Construct an Adam optimizer that will adjust the texture parameters
    opt = Adam(disp_tex_params, lr=0.002)

    time_a = time.time()

    for it in range(args.iter):
        # Perform a differentiable rendering of the scene
        image = render(scene,
                       optimizer=opt,
                       spp=2,
                       unbiased=True,
                       pre_render_callback=apply_displacement)

        write_bitmap(args.out + 'out_%03i.png' % it, image, crop_size)

        # Objective: MSE between 'image' and 'image_ref'
        ob_val = ek.hsum(ek.sqr(image - image_ref)) / len(image)

        # Back-propagate errors to input parameters
        ek.backward(ob_val)

        # Optimizer: take a gradient step -> update displacement map
        opt.step()

        # Compare iterate against ground-truth value
        err_ref = ek.hsum(ek.sqr(disp_tex_data_ref - disp_tex.eval_1(mesh_si)))
        print('Iteration %03i: error=%g' % (it, err_ref[0]), end='\r')

    time_b = time.time()

    print()
    print('%f ms per iteration' % (((time_b - time_a) * 1000) / args.iter))
