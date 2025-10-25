'''
An image pipeline with screen shot or file sources.

This needs some external programs to fully function:

- Screenshot based on selection requires the "slop" program is required.

- To include window title in a the EXIF comment tag of a grabbed image, "xdotool"
  is needed.  To include firefox's URL, "xclip" is also needed.
'''
import io
import sh
import os
import sys
from tomllib import loads as toml_loads, TOMLDecodeError
from pathlib import Path
import shlex
import click
import requests
import tempfile
import datetime
import webbrowser
from collections import defaultdict
from contextlib import redirect_stdout
from functools import update_wrapper
from PIL import Image
from PIL import ImageGrab
from PIL.ExifTags import TAGS

# This is HEAVILY inspired by the Click example imagepipe.py.
#
# My thanks to the Click team for a really excellent package!
#
# Here we add screenshot and some other ways to dispatch a file and remove some
# of the image processing functions from the imagepipe.py example.

def config_home():
    '''
    Return the default configuration home
    '''
    try:
        return Path(os.environ['XDG_CONFIG_HOME'])
    except KeyError:
        return Path.home() / ".config"



# used for formatting to gracefully handle missing keys.
class Default(dict):
    def __missing__(self, key):
        return ''


def format_string(string, **params):
    """
    Formats a string using str.format_map(), augmented with current time parameters.

    Any key in the string that is not in the params is replaced with a space.

    Args:
        string (str): The format string.
        **params: Additional parameters to use for formatting.
                  These will override time parameters if names conflict.

    Returns:
        str: The formatted string.
    """
    now = datetime.datetime.now()
    time_params = {
        'year': now.year,
        'month': now.month,
        'day': now.day,
        'hour': now.hour,
        'minute': now.minute,
        'second': now.second,
        'iso': now.isoformat(timespec='seconds'),
        # dMonth names
        'month_full': now.strftime('%B'),  # e.g., 'October'
        'month_abbr': now.strftime('%b'),  # e.g., 'Oct'
        # Day names
        'day_full': now.strftime('%A'),    # e.g., 'Friday'
        'day_abbr': now.strftime('%a'),    # e.g., 'Fri'
    }
    
    # Combine time_params and user-provided params.
    # User params take precedence in case of key conflicts.
    combined_params = {**time_params, **params}
    
    return string.format_map(Default(**combined_params))

def image_params(image):
    '''
    Return metadata about image as dict.
    '''
    params = dict(image.info)
    filename = getattr(image, "filename", "")
    params['filename'] = filename
    mode = image.mode
    params['mode'] = mode
    params['width'], params['height'] = image.size

    if filename in ("", "-"):
        if mode == "RGBA":
            ext = ".png"
        elif hasattr(image, "layers"):
            ext = ".jpg"
        else:
            ext = ".png"
    else:
        _, ext = os.path.splitext(filename)
    params['ext'] = ext
    return params

def run_self(args):
    '''
    Run this program with args
    '''
    click.echo(f"Running: python {sys.argv[0]} {' '.join(args)}")

    f = io.StringIO()
    with redirect_stdout(f):
        try:
            ctx = cli.make_context(sys.argv[0], args=args)
            cli.invoke(ctx)
        except Exception as e:
            click.echo(f"An error occurred during pipeline execution: {e}", err=True)

    out = f.getvalue().strip()
    if out:
        click.echo(out)

default_config_filename = config_home() / "imp" / "config.toml"

@click.group()
@click.option("-c","--config",
              default=default_config_filename,
              help="Configuration file in TOML syntax defining pipelines and nodes")
@click.pass_context
def cli(ctx, config):
    '''
    An image processing pipeline.
    '''
    try:
        ctx.obj = {
            "config": toml_loads(open(config).read()),
            "config_filename": config
        }
        
    except TOMLDecodeError as e:
        click.echo(f"Error parsing TOML configuration: {e}", err=True)
    except Exception:
        ctx.obj = dict()




@cli.group(chain=True)
def pipe():
    '''
    An image pipeline with screen shot or file sources.

    imp [cmd [options]] ...

    This program needs some external programs to fully function:

    - Screenshot based on selection requires the "slop" program is required.

    - To include window title in a the EXIF comment tag of a grabbed image,
      "xdotool" is needed.  To include firefox's URL, "xclip" is also needed.
    '''
    pass


@pipe.result_callback()
def process_commands(processors):
    stream = ()
    for processor in processors:
        stream = processor(stream)
    for _ in stream:
        pass    

def processor(f):
    '''
    Turn a command into a stream processor.
    '''
    def new_func(*args, **kwargs):
        def processor(stream):
            return f(stream, *args, **kwargs)

        return processor

    return update_wrapper(new_func, f)

def generator(f):
    '''
    Turna processor into a generator by first implicitly forwarding any
    images in the pipeline and then calling the function to generate new images.
    '''

    @processor
    def new_func(stream, *args, **kwargs):
        yield from stream
        yield from f(*args, **kwargs)

    return update_wrapper(new_func, f)

@pipe.command("open")
@click.option(
    "-i",
    "--input",
    "inputs",
    type=click.Path(),
    multiple=True,
    help="The image file to input to a pipeline.",
)
@generator
def pipe_open(inputs):
    """
    Loads one or multiple images for processing.  The input parameter
    can be specified multiple times to load more than one image.
    """
    for image in inputs:
        try:
            click.echo(f"Opening '{image}'")
            if image == "-":
                img = Image.open(click.get_binary_stdin())
                img.filename = "-"
            else:
                img = Image.open(image)
            yield img
        except Exception as e:
            click.echo(f"Could not open image '{image}': {e}", err=True)


@pipe.command("grab")
@click.option(
    "-m",
    "--method",
    default="select",
    type=click.Choice(["select", "screen"]),
    help="How to acquire the screen shot",
    show_default=True,
)
@click.option(
    "-c"
    "--comment",
    "comment",
    type=str,
    default='{title} {url}',
    help="A pattern string to add as a 'comment' EXIF tag",
    show_default=True
)
@click.option(
    "-i",
    "--info",
    default="title,url",
    help="Comma spearated list of metadata info types to try to grab",
    show_default=True
)
@generator
def pipe_grab(method, info, comment):
    """
    Generate an image by grabbing a screen shot and potentially metadata info.

    The "--method" determines how a screenshot is acquired.  The method is saved
    as "grab_method" entry in Image.info.

    The "select" method requires the "slop" program to be installed.  When
    launched, user can select a window (click twice) or a rectangle (click,
    drag, click).  Note, "slop" works best when a compositer (eg compton) is
    running.  Here we assume the crosshair shader is in ~/.config/slop/.  See
    https://github.com/naelstrof/slop

    Each entry in --info will attempt to grab additional information and save it
    to the Image.info dictionary.  Keys are named as the info name with "grab_"
    prefixed.

    If a --comment pattern is given it is interpolated on contents of the
    Image.info dictionary.  The result is added as "grab_comment to Image.info.
    """
    # fixme: make all this configurable
    click.echo(f'Screenshot via "{method}"')
    if method == "screen":
        img = ImageGrab.grab()
    else:
        text = sh.slop(["--color=0.0,0.8,0.0,0.8",
                        #"--highlight",
                        "--nodrag",
                        "--shader", "crosshair"
                        ])
        size,x,y = text.strip().split("+")
        w,h = size.split("x")
        x,y,w,h = map(int, (x,y,w,h))
        # left, up, right, down
        bbox = [x, y, x+w, y+h]
        print(f'{bbox=}')
        img = ImageGrab.grab(bbox)

    # act as if image came from stdin
    img.filename = "-"


    if "title" in info:
        title = sh.xdotool(["getwindowfocus", "getwindowname"]).strip()
        img.info["grab_title"] = title

    if "url" in info:
        curwin = sh.xdotool("getwindowfocus").strip()
        foxwins = sh.xdotool(["search", "--onlyvisible", "--classname", "Navigator"], _ok_code=(0, 1))
        if curwin in foxwins:
            # massively ugly hack! :)
            sh.xdotool(["key", "--window", curwin, "--delay", "20", "--clearmodifiers", "ctrl+l", "ctrl+c", "Escape"])
            url = sh.xclip(["-o", "-selection", "clipboard"]).strip()
            img.info["grab_url"] = url
    if comment:
        params = image_params(img)
        grab_comment = format_string(comment, **params)
        click.echo(f'Setting grab comment: "{grab_comment}"')
        img.info["grab_comment"] = grab_comment

    yield img


def do_image_notify(msg, image):
    '''
    Notify about image with msg.
    '''
    thumb = image.copy()
    thumb.thumbnail((100,100))
    if image.filename == "-":
        ext = ".png"
    else:
        _,ext = os.path.splitext(image.filename)        
    fp,thumbfile = tempfile.mkstemp(ext)
    thumb.save(thumbfile)
    ns = sh.Command("notify-send")
    ns([f'--icon={thumbfile}', msg])
    if os.path.exists(thumbfile):
        os.close(fp)        # without this it will leak open files
        os.remove(thumbfile)

@pipe.command("xclip")
@click.option(
    "-m",
    "--message",
    "message",
    default="{upload_url}",
    help="A format string to place in the clipboard",
    show_default=True,
)
@click.option(
    "-s",
    "--selection",
    default="primary",
    help="Which X selection to use",
    show_default=True,
    type=click.Choice(["primary", "secondary", "clipboard"]),
)
@processor
def pipe_xclip(images, message, selection):
    '''
    Apply Image.info to message and send it to the X11 selection.
    '''
    lines = list()
    for count, image in enumerate(images):
        line = format_string(message, count=count, **image_params(image))
        lines.append(line)
        yield image
    text = '\n'.join(lines)
    sh.xclip(['-in', '-selection', selection], _in=text)
        
        
    

@pipe.command("notify")
@click.option(
    "-c",
    "--comment",
    "comment",
    type=str,
    default="Image number {count}\n{filename}\n{upload_url}\n{width}x{height}",
    help="A pattern string to add as a 'comment' EXIF tag",
    show_default=True
)
@processor
def pipe_notify(images, comment):
    '''
    Send a notification consisting of a message formed by applying comment
    with f-string parameters to the Image.info dictionary as well as a count of
    the image in the stream and the Image.filename, width, and height
    '''
    comment = comment.replace('<p>', '\n')
    for count, image in enumerate(images):

        params = image_params(image)
        msg = format_string(comment, count=count, **params)
        click.echo(msg)
        do_image_notify(msg, image)
        yield image

    


@pipe.command("save")
@click.option(
    "-f",
    "--filename",
    default="processed-{count:04}.png",
    type=click.Path(),
    help="The format for the filename.",
    show_default=True,
)
@click.option(
    "-c"
    "--comment",
    "comment",
    type=str,
    default='{grab_title} {grab_url}',
    help="A pattern string to add as a 'comment' EXIF tag",
    show_default=True
)
@processor
def pipe_save(images, comment, filename):
    '''
    Saves images to files named according to filename pattern.

    The following parameters may be used in f-string format codes.

    - count :: index-0 count of files input to the save node.
    - width :: width in pixels.
    - height :: height in pixels.
    - mode :: image mode (RGB, etc).

    If --comment is given, it is an f-string interpolated against the contents
    of the Image.info dictionary and saved as the "Comment" EXIF tag.

    The image object will have its .filename set to the saved file.
    '''
    for count, image in enumerate(images):
        try:
            params = image_params(image)
            fn = format_string(filename, count=count, **params)
                                 
            click.echo(f"Saving '{image.filename}' as '{fn}'")

            image.save(fn)
            image.filename = fn

            if comment:
                params = image_params(image)
                click.echo(f'{params=}')
                exif_comment = format_string(comment, count=count, **params)
                click.echo(f'Adding EXIF comment: "{exif_comment}" to {fn}')
                # it's approximately impossible to do this with PIL!  
                sh.exiftool([f'-comment={exif_comment}', fn])

            yield image

        except Exception as e:
            click.echo(f"Could not save image '{image.filename}': {e}", err=True)


@pipe.command("resize")
@click.option("-w", "--width", type=int, help="The new width of the image.")
@click.option("-h", "--height", type=int, help="The new height of the image.")
@processor
def pipe_resize(images, width, height):
    """Resizes an image by fitting it into the box without changing
    the aspect ratio.
    """
    for image in images:
        w, h = (width or image.size[0], height or image.size[1])
        click.echo(f"Resizing '{image.filename}' to {w}x{h}")
        image.thumbnail((w, h))
        yield image


@pipe.command("display")
@processor
def pipe_display(images):
    """Opens all images in an image viewer."""
    for image in images:
        click.echo(f"Displaying '{image.filename}'")
        image.show()
        yield image

@pipe.command("browser")
@processor
def pipe_browser(images):
    """Opens the upload_url to a web browser, if it exists."""
    for image in images:
        url = image.info.get("upload_url", "")
        if url:
            webbrowser.open(url)
        else:
            click.echo(f'No upload_url in info: {image.info}')
        yield image

@pipe.command("check")
@click.option(
    "--info",
    default='upload_url',
    help='An Image.info key that must exist',
    show_default=True
)
@click.option(
    "--okay",
    default='{upload_url}',
    help='A notify string if info exists',
    show_default=True
)
@click.option(
    "--fail",
    default='Upload failed',
    help='A notify string if info does not exist',
    show_default=True
)
@processor
def pipe_check(images, info, okay, fail):
    """Check for key in info, if there notify with okay else with fail."""
    for image in images:
        if info in image.info:
            msg = okay
        else:
            msg = fail
        msg = format_string(msg, **image_params(image))
        do_image_notify(msg, image)
        yield image


@pipe.command("0x0")
@click.option(
    "--url",
    default = 'https://0x0.st',
    help="URL of an 0x0 service",
    show_default=True
)
@processor
def pipe_0x0(images, url):
    '''
    Upload the image to 0x0.

    This adds the returned URL to 'upload_0x0_url' and 'upload_url' Image.info
    items.
    '''

    for image in images:
        form="JPEG"
        if image.filename.lower().endswith('.png'):
            form="PNG"
        if image.filename.lower().endswith(('.jpg', '.jpeg')):
            form="JPEG"
        bytes = io.BytesIO()
        image.save(bytes, format=form)
        bytes.seek(0)
        files = dict(file = bytes)
        headers = { "User-Agent": "brettviren/imp image pipeline processor" }
        response = requests.post(url, files=files, headers=headers)
        print(response.text)
        image.info['upload_0x0_url'] = response.text.strip()
        image.info['upload_url'] = response.text.strip()
        yield image



@cli.command("config")
@click.pass_context
def cmd_config(ctx):
    '''
    Print default location of configuration file.
    '''
    click.echo(default_config_filename)

@cli.command("list")
@click.pass_context
def cmd_list(ctx):
    '''
    List pipelines defined in the configuration file
    '''
    config = ctx.obj["config"]

    try:
        pipelines_data = config.get("pipelines", {})
        nodes_data = config.get("nodes", {})

        if not pipelines_data:
            click.echo("No pipelines found in the configuration.")
            return

        for name, data in pipelines_data.items():
            description = data.get("description", "No description provided.")
            click.echo(f'  Pipeline: "{name}"')
            click.echo(f"  Description: {description}")
            pipe_node_names_raw = data.get("nodes")
            if isinstance(pipe_node_names_raw, str):
                node_names = shlex.split(pipe_node_names_raw)
            elif isinstance(pipe_node_names_raw, list):
                node_names = pipe_node_names_raw
            for node_name in node_names:
                node_def = dict(nodes_data.get(node_name))
                cmd_name = node_def.pop('node')
                cmd_desc = node_def.pop("description", "")
                args = ', '.join([f'{k}="{v}"' for k,v in node_def.items()])
                click.echo(f'    Node "{node_name}" {cmd_name}({args}) {cmd_desc}')

            click.echo("-" * 20)
            

    except Exception as e:
        click.echo(f"An unexpected error occurred: {e}", err=True)
        
    
# Helper to parse extra CLI args into node-specific options
def wash_args(args):
    """
    Parses a list of raw CLI arguments for node-prefixed options.

    Format is:

      <node_name>:<option>=<value>

    The <option> should be the long-option name, with no "--" prefix.

    Returns dict keyed by <node_name>'s with values dicts keyed by "--<option>"
    giving "<value>".
    """
    parsed = defaultdict(dict)
    pipe_name = None
    for one in args:
        if ":" not in one:
            pipe_name = one
            continue
        node_name, keyval = one.split(":")
        key,val = keyval.split("=")
        parsed[node_name][f'{key}'] = val
    return pipe_name, parsed


@cli.command("run")
@click.argument('args', nargs=-1, type=click.UNPROCESSED) # Capture all unconsumed args
@click.pass_context
def cmd_run(ctx, args):
    """Run a specific pipeline defined in config."""
    pipeline_name, node_external_options = wash_args(args)

    # click.echo(f"Attempting to run pipeline: {pipeline_name}")

    config = ctx.obj["config"]

    try:

        pipelines_data = config.get("pipelines", {})
        nodes_data = config.get("nodes", {})

        pipeline_def = pipelines_data.get(pipeline_name)
        if not pipeline_def:
            raise click.BadParameter(f"Pipeline '{pipeline_name}' not found in configuration.")

        # first try it as a simple command line
        cmdline = pipeline_def.get("pipeline", "")
        if cmdline:
            command_fragments = shlex.split(cmdline)

        else: # Determine the sequence of node names
            pipe_node_names_raw = pipeline_def.get("nodes")
            if isinstance(pipe_node_names_raw, str):
                node_names = shlex.split(pipe_node_names_raw)
            elif isinstance(pipe_node_names_raw, list):
                node_names = pipe_node_names_raw
            else:
                raise click.BadParameter(f"Pipeline '{pipeline_name}' has invalid 'pipe' definition. Must be a string or list.")

            if not node_names:
                click.echo(f"Pipeline '{pipeline_name}' has no nodes defined. Doing nothing.")
                return

            # Construct the command-line arguments for the 'pipeline' group
            command_fragments = []
            for i, node_name in enumerate(node_names):
                node_def = nodes_data.get(node_name)
                if not node_def:
                    raise click.BadParameter(f"Node '{node_name}' referenced in pipeline '{pipeline_name}' not found in 'nodes' table.")

                command_func_name = node_def.pop("node")
                if not command_func_name:
                    raise click.BadParameter(f"Node '{node_name}' in pipeline '{pipeline_name}' has no 'node' (Click command) defined.")

                # Append the command function name
                command_fragments.append(command_func_name)

                node_def.pop("description", "")
                node_def.update(node_external_options.get(node_name, {}))

                # Iterate through other attributes in node_def and treat them as options
                for attr_key, attr_value in node_def.items():
                    if attr_key not in ["node", "description"]:
                        # Convert 'value' to '--value'
                        long_option_name = f"--{attr_key}"
                        command_fragments.append(long_option_name)
                        command_fragments.append(str(attr_value))


        # Now, construct the full CLI arguments to pass to cli.invoke
        # The first element is always 'pipeline' to target the chainable group
        full_cli_args = []
        if "config_filename" in ctx.obj:
            full_cli_args += ["--config", ctx.obj['config_filename']]

        full_cli_args += ['pipe'] + command_fragments
        pipeline_fragment_string_for_display = ' '.join(command_fragments)

        run_self(full_cli_args)

    except click.BadParameter as e:
        click.echo(f"Configuration error: {e}", err=True)
    except Exception as e:
        click.echo(f"An unexpected error occurred: {e}", err=True)

@cli.command("rofi")
@click.pass_context
def cmd_rofi(ctx):
    '''
    Generate a rofi listing
    '''
    config = ctx.obj["config"]

    pipelines = config["pipelines"]

    guess_font_height = 60
    height = guess_font_height * len(pipelines)
    width = 1000
    window = "window {width: %d; height: %d;}"%(width,height)

    lines = list()
    pipes = list()
    for pipeline, pipedata in pipelines.items():
        pipes.append(pipeline)
        lines.append(pipedata['nodes'] + '\n')
    text = ''.join(lines)

    try:
        index = sh.rofi(["rofi","-dmenu",
                         "-format", "i", # return index
                         "-theme-str", window,
                         "-p", "imp pipeline"],
                        _in=text)
    except Exception as err:
        print(f'rofi aborted given input:\n{text}')
        print(err)
        return 1

    index = int(index.strip())
    pipe = pipes[index]
    print(f'{index=} {pipe=}')

    args = list()
    if "config_filename" in ctx.obj:
        args += ["--config", ctx.obj['config_filename']]
    args += ["run", pipe]
    run_self(args)
