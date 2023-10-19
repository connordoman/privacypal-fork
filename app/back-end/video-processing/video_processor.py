import cv2 as cv, os, time, multiprocessing as mp, numpy as np, subprocess as sp, random, boto3

def extract_frames(path: str) -> str:   # likely legacy as it seems performance is much better when we're doing all operations in RAM rather than saving/loading from disk
    """
    Extracts and save all frames of a video into a new folder and return the directory's name
    File pointed to by `path`, opencv and ffmpeg do the frame extraction.
    """
    fps = cv.VideoCapture(path).get(cv.CAP_PROP_FPS)
    directory = f"temp_{time.time()}"   # make the directory name unique
    os.mkdir(directory)
    os.system(f"ffmpeg -i {path} -vf fps={fps} {directory}/frame%04d.jpg")
    return directory

def blur_frame(img, rects: list, r: int = 50):
    """
    Loads an image and applies a blur on all regions specified by `rects`.
    `img` is an opencv image (ie essentially an np array).
    `rects` is of the form [[x, y, w, h], [x, y, w, h], ...] where xy is the top left corner of the rectangle
    """
    H, W = img.shape[:2]    # height and width of image
    r = int(W / r)          # define a blur radius that scales with the image, definitely want to change and fine tune this in the future
    # if len(rects) == 0:     # if no rectangles passed, init a simple checker pattern blur for testing
    #     w, h = int(W / 2), int(H / 2)
    #     rects = [[0, 0, w, h], [w, h, w, h]]
    for rect in rects:      # for every blurring zone, blur the zone and update the image
        x, y, w, h = rect[:4]       # init loop variables, if rect is longer than 4 elements, discard the extra elements
        section = img[y:y+h, x:x+w] # cut out a section with numpy indice slicing
        img[y:y+h, x:x+w] = [0, 0, 255] #cv.blur(section, (r, r))    # blur the section and replace the indices with the now blurred section
    return img

def process_FULL(src: str, tmp: str, out: str):
    """
    Takes 3 file paths, `src`, `tmp`, and `out`. Loads `src` into memory, applies a blur on every frame,
    and exports to `tmp`. Finally, audio is copied from `src` and video is copied from `tmp` to `out`
    to product the final video.

    Currently blur_frame() has no `rects` passed to it, this will be updated when we hook up to AWS Rekognition.
    Additionally, this function has NO interpolation and assumes ALL blur coordinates for every frame will be provided.
    """
    input = cv.VideoCapture(src)        # load input video 
    fps = input.get(cv.CAP_PROP_FPS)    # read our fps, we need this to set the output video properties
    hasNext, img = input.read()         # get next frame
    h, w = img.shape[:2]                # get height and width of our input video to set frameSize for our output video
    output = cv.VideoWriter(filename=tmp, fps=fps, frameSize=(w, h), fourcc=cv.VideoWriter_fourcc(*'mp4v'))     # init our video output
    while hasNext:      # hasNext is a truthy int returned from input.read() that indicates if there's another frame in the video
        output.write(blur_frame(img))   # blur image and write it to our output video
        hasNext, img = input.read()     # get next frame
    input.release()     # release input io lock
    output.release()    # release output io lock
    # the command we now need to run to combine the audio from `src` and the video from `tmp` is:
    # 'ffmpeg -y -i {tmp} -i {src} -c copy -map 0:v:0 -map 1:a:0 out'. This looks confusing and scary but here's the breakdown:
    #  - the '-y' operator answers "yes" to any potential "overwrite files?" prompts. May be undesirable in the future if concurrency/multiple encodings occur at the same time
    #  - with each '-i' we specify an input video
    #  - "-c copy" expands to "-codec copy" which means we don't re-encode either the audio or the video, we simply copy/dump it to the output file (much faster, but also less desirable if we want a standard output format)
    #  - "-map 0:v:0" tells ffmpeg we want our 0th index input video's 0th video stream (our first input video is `tmp` which contains our blurred video, so we want that video stream in our final output)
    #  - "-map 1:a:0" tells ffmpeg we want our 1th index input video's 0th audio stream (our second input video is `src` which contains unblurred video but original audio which we want in our final output)
    #  - and finally we specify the output file `out`
    sp.Popen(["ffmpeg", "-y", "-i", tmp, "-i", src, "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", out], stdout=sp.PIPE, stderr=sp.STDOUT)
    print("Done.")  # sp.Popen will wait until the process is complete before continuing

def get_frames(path: str) -> list:
    """
    Reads all frames from the video specified by `path` into an array of OpenCV images.
    """
    out = []
    input = cv.VideoCapture(path)
    hasNext, img = input.read()
    while hasNext:
        out.append(img)
        hasNext, img = input.read()
    return out

def calc_vector(x1: int, y1: int, x2: int, y2: int, w: int, h: int, n: int) -> list:
    """
    Takes a start point (`x1`, `y1`) and an end point (`x2`, `y2`) and returns
    a list of `n` points evenly distributed between the 2 points (exclusive).
    Copies the width and height specified in the parameters (`w` and `h`) to the output so
    that the output consists of a list of lists where each inner list is of the
    form [`x`, `y`, `w`, `h`].

    Example input: x1, y1, x2, y2, w, h, n = 0, 0, 10, 10, 3, 6, 9
    Example output: [[1, 1, 3, 6], [2, 2, 3, 6], [3, 3, 3, 6], [4, 4, 3, 6], [5, 5, 3, 6],
    [6, 6, 3, 6], [7, 7, 3, 6], [8, 8, 3, 6], [9, 9, 3, 6]]
    """
    stepx, stepy = (x2 - x1) / (n + 1), (y2 - y1) / (n + 1)
    out = []
    for i in range(1, n + 1):
        out.append([int(x1 + stepx * i), int(y1 + stepy * i), w, h])
    return out

def calc_vector_size(x1: int, y1: int, x2: int, y2: int, w1: int, h1: int, w2: int, h2: int, n: int) -> list:
    """
    Essentially the same as `calc_vector` but it calculates the interpolation of the box size as well.
    Takes a start point (`x1`, `y1`) and an end point (`x2`, `y2`) and returns
    a list of `n` points evenly distributed between the 2 points (exclusive).
    Also calculates the shift in width and height as passed in (`w1`, `h1`) and (`w2`, `h2`).

    Example input: x1, y1, x2, y2, w, h, n = 0, 0, 3, 3, 4, 3, 7, 6, 2
    Example output: [[1, 1, 5, 4], [2, 2, 6, 5]]
    """
    n += 1  # all calculations require n increasing by 1 so we'll just do it beforehand
    stepx, stepy = (x2 - x1) / n, (y2 - y1) / n
    stepw, steph = (w2 - w1) / n, (h2 - h1) / n
    out = []
    for i in range(1, n):
        out.append([int(x1 + stepx * i), int(y1 + stepy * i), int(w1 + stepw * i), int(h1 + steph * i)])
    return out

def calc_vector_size_BOX(box1: list, box2: list, n) -> list:
    """
    Method overload for `calc_vector_size` but Python doesn't support proper method overloading which is why the `_BOX` suffix.
    """
    return calc_vector_size(box1[0], box1[1], box2[0], box2[1], box1[2], box1[3], box2[2], box2[3], n)

def pick_point(img):
    """
    Temporary function to take an image and randomly pick a point on it, for testing the interpolation workflow
    """
    h, w = img.shape[:2]
    return [random.randint(0, w - 1), random.randint(0, h - 1)]

def process_INTERPOLATE(src: str, tmp: str, out: str, keyframe_interval: float = 0.5):
    """
    Processes a final video from start to finish using interpolation techniques.

    Takes 3 paths, `src` for the original video source, `tmp` for where the temporary blurred but
    no audio video will be stored, and `out` for the final video. Also takes `keyframe_interval`,
    a float that describes the number of seconds between keyframes. A lower `keyframe_interval` will
    have higher accuracy at the cost of more Rekognition requests (and therefore slower execution).
    If `keyframe_interval` is not specified, it defaults to 0.5 (a half second).
    """
    frames = get_frames(src)
    n = len(frames)
    H, W = frames[0].shape[:2]
    fps = cv.VideoCapture(src).get(cv.CAP_PROP_FPS)
    frame_gap = int(fps * keyframe_interval)    # number of frames between keyframes

    # get our faces from rekognition
    known_boxes = []# [pick_point(frames[0]) for i in range(int(n / frame_gap) + 1)]
    for i in range(0, n + 1, frame_gap):
        box = get_face(frames[i])
        known_boxes.append(box)
    
    # interpolate the positions of the faces based on `frame_gap`
    boxes = []
    for i in range(len(known_boxes) - 1):
        start, end = known_boxes[i], known_boxes[i + 1]
        boxes += [start]
        boxes += calc_vector_size_BOX(start, end, frame_gap - 1)
    boxes += [known_boxes[-1]]
    while len(boxes) < n:
        boxes.append(boxes[-1])

    # use `boxes` to blur the frames and output the finished video
    output = cv.VideoWriter(filename=tmp, fps=fps, frameSize=(W, H), fourcc=cv.VideoWriter_fourcc(*'mp4v'))     # init our video output
    for i in range(n):
        output.write(blur_frame(frames[i], [boxes[i]]))
    output.release()    # release output io lock
    # the command we now need to run to combine the audio from `src` and the video from `tmp` is:
    # 'ffmpeg -y -i {tmp} -i {src} -c copy -map 0:v:0 -map 1:a:0 out'. This looks confusing and scary but here's the breakdown:
    #  - the '-y' operator answers "yes" to any potential "overwrite files?" prompts. May be undesirable in the future if concurrency/multiple encodings occur at the same time
    #  - with each '-i' we specify an input video
    #  - "-c copy" expands to "-codec copy" which means we don't re-encode either the audio or the video, we simply copy/dump it to the output file (much faster, but also less desirable if we want a standard output format)
    #  - "-map 0:v:0" tells ffmpeg we want our 0th index input video's 0th video stream (our first input video is `tmp` which contains our blurred video, so we want that video stream in our final output)
    #  - "-map 1:a:0" tells ffmpeg we want our 1th index input video's 0th audio stream (our second input video is `src` which contains unblurred video but original audio which we want in our final output)
    #  - and finally we specify the output file `out`
    sp.Popen(["ffmpeg", "-y", "-i", tmp, "-i", src, "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", out], stdout=sp.PIPE, stderr=sp.STDOUT)
    print("Done.")  # sp.Popen will wait until the process is complete before continuing

def img_to_bytes(img) -> bytes:
    """
    Simple function to take an opencv `img` and return its `bytes` representation.
    Used to send images to Rekognition.
    """
    _, img_arr = cv.imencode(".jpg", img)
    return img_arr.tobytes()

def get_face(img) -> list:
    """
    Takes an image, converts it to `bytes`, sends a request to rekognotion, parses the JSON response,
    and returns the `xywh` coordinates in the form [`x`, `y`, `w`, `h`].
    `img` is an opencv image (essentially a np array)
    """
    H, W = img.shape[:2]    # get the height and width of the image
    client = boto3.client("rekognition")    # request a client of the type 'rekognition' from aws services
    response = client.detect_faces(Image={"Bytes": img_to_bytes(img)})  # run detect_faces and collect response
    facedetails = (response["FaceDetails"])[0]  # facedetails is of type dict
    box = facedetails["BoundingBox"]            # box is of type dict
    x, y, w, h = box["Left"] * W, box["Top"] * H, box["Width"] * W, box["Height"] * H   # use H/W to scale the percents back to pixel values
    return [int(i) if i > 0 else 0 for i in [x, y, w, h]]

if __name__ == "__main__":
    boto3.setup_default_session(profile_name="paul")    # load the SSO profile/session
    
    base = "app/back-end/video-processing"
    src, tmp, out = f"{base}/videos/paul test phone.mp4", f"{base}/out/temp.mp4", f"{base}/out/final.mp4"
    process_INTERPOLATE(src, tmp, out, 0.1)
    