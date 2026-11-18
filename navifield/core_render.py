"""Headless retained-field rendering primitives."""

def _render_one(
    slc,
    scalar_name: str,
    axis_name: str,
    out_path: str,
    cmap: str = "viridis",
    bar_title: str | None = None,
    time_tag: str = "",
    association: str | None = None,
    isometric: bool = False,
    annotation: str | None = None,
    scalar_range: tuple[float, float] | None = None,
) -> None:
    import pyvista as pv

    if association not in {None, 'point', 'cell'}:
        raise ValueError('render association must be point or cell')
    if scalar_range is not None:
        import math
        if (len(scalar_range) != 2 or not all(math.isfinite(value) for value in scalar_range)
                or scalar_range[0] > scalar_range[1]):
            raise ValueError('render scalar range must be finite and ordered')
    plotter = pv.Plotter(off_screen=True, window_size=[1280, 800])
    try:
        _render_one_plotter(plotter, slc, scalar_name, axis_name, out_path, cmap,
                            bar_title, time_tag, association, isometric, annotation, scalar_range)
    finally:
        plotter.close()

def _render_one_plotter(plotter, slc, scalar_name, axis_name, out_path, cmap,
                        bar_title, time_tag, association, isometric, annotation, scalar_range):
    plotter.set_background("white")
    plotter.add_mesh(
        slc,
        scalars=scalar_name,
        cmap=cmap,
        **({'preference': association} if association is not None else {}),
        **({'clim': scalar_range} if scalar_range is not None else {}),
        show_scalar_bar=scalar_range is None or scalar_range[0] != scalar_range[1],
        scalar_bar_args={
            "title": bar_title or scalar_name,
            "color": "black",
            "title_font_size": 18 if isometric else 36,
            "label_font_size": 14 if isometric else 28,
            "n_labels": 5,
            "fmt": "%.3g",
            "vertical": True,
            "position_x": 0.86 if isometric else 0.90,
            "position_y": 0.10,
            "width": 0.07,
            "height": 0.80,
        },
    )

    annotation = f"{axis_name} slice" if annotation is None else annotation
    if time_tag:
        annotation += f"   t = {time_tag}"
    plotter.add_text(annotation, position="upper_left", font_size=14 if isometric else 22, color="black")

    if isometric:
        plotter.view_isometric()
    elif axis_name == "Y":
        plotter.view_xz()
    else:
        plotter.view_xy()

    plotter.reset_camera()
    plotter.camera.zoom(0.85 if isometric else 1.4)
    plotter.screenshot(out_path)
