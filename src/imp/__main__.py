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
import click
import requests
import tempfile
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

# used for formatting to gracefully handle missing keys.
class Default(dict):
    def __missing__(self, key):
        return ''


@click.group(chain=True)
def cli():
    '''
    An image pipeline with screen shot or file sources.

    imp [cmd [options]] ...

    This program needs some external programs to fully function:

    - Screenshot based on selection requires the "slop" program is required.

    - To include window title in a the EXIF comment tag of a grabbed image,
      "xdotool" is needed.  To include firefox's URL, "xclip" is also needed.
    '''
    pass


@cli.result_callback()
def process_commands(processors):
    stream = ()
    for processor in processors:
        stream = processor(stream)
    for _ in stream:
        pass    

def processor(f):
    def new_func(*args, **kwargs):
        def processor(stream):
            return f(stream, *args, **kwargs)

        return processor

    return update_wrapper(new_func, f)

def generator(f):

    @processor
    def new_func(stream, *args, **kwargs):
        yield from stream
        yield from f(*args, **kwargs)

    return update_wrapper(new_func, f)

@cli.command("open")
@click.option(
    "-i",
    "--input",
    "inputs",
    type=click.Path(),
    multiple=True,
    help="The image file to input to a pipeline.",
)
@generator
def cmd_open(inputs):
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


@cli.command("grab")
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
    default=None,
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
def cmd_grab(method, info):
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
                        "--highlight",
                        "--nodrag",
                        "--shader", "crosshair"])
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
        grab_comment = comment.format_map(Default(**img.info))
        click.echo(f'Setting grab comment: "{grab_comment}"')
        img.info["grab_comment"] = grab_comment

    yield img


@cli.command("notify")
@click.option(
    "-c"
    "--comment",
    "comment",
    type=str,
    default="Image number {count}\n{filename}\n{upload_url}\n{width}x{height}",
    help="A pattern string to add as a 'comment' EXIF tag",
    show_default=True
)
@processor
def cmd_notify(images, comment):
    '''
    Send a notification consisting of a message formed by applying comment
    with f-string parameters to the Image.info dictionary as well as a count of
    the image in the stream and the Image.filename, width, and height
    '''
    for count, image in enumerate(images):

        width, height = image.size
        msg = comment.format_map(Default(width=width, height=height,
                                         count=count, filename=image.filename,
                                         **image.info))
        click.echo(msg)
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
        yield image

    


@cli.command("save")
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
    default="Grabbed from: {title} {url}",
    help="A pattern string to add as a 'comment' EXIF tag",
    show_default=True
)
@processor
def cmd_save(images, comment, filename):
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
            width, height = image.size
            fn = filename.format_map(Default(count=count,mode=image.mode,
                                             width=width, height=height))
                                 
            click.echo(f"Saving '{image.filename}' as '{fn}'")

            image.save(fn)
            image.filename = fn

            if comment:
                exif_comment = comment.format_map(Default(**image.info))
                click.echo(f'Adding EXIF comment: "{exif_comment}" to {fn}')
                # it's approximately impossible to do this with PIL!  
                sh.exiftool([f'-comment={exif_comment}', fn])

            yield image

        except Exception as e:
            click.echo(f"Could not save image '{image.filename}': {e}", err=True)


@cli.command("resize")
@click.option("-w", "--width", type=int, help="The new width of the image.")
@click.option("-h", "--height", type=int, help="The new height of the image.")
@processor
def cmd_resize(images, width, height):
    """Resizes an image by fitting it into the box without changing
    the aspect ratio.
    """
    for image in images:
        w, h = (width or image.size[0], height or image.size[1])
        click.echo(f"Resizing '{image.filename}' to {w}x{h}")
        image.thumbnail((w, h))
        yield image


@cli.command("display")
@processor
def cmd_display(images):
    """Opens all images in an image viewer."""
    for image in images:
        click.echo(f"Displaying '{image.filename}'")
        image.show()
        yield image


@cli.command("0x0")
@click.option(
    "--url",
    default = 'https://0x0.st',
    help="URL of an 0x0 service",
    show_default=True
)
@processor
def cmd_0x0(images, url):
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
