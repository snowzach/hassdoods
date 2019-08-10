"""Support for the DOODS service."""
import logging
import os
import sys
import requests
import base64
import time
import voluptuous as vol

from homeassistant.components.image_processing import (
    CONF_CONFIDENCE,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_SOURCE,
    PLATFORM_SCHEMA,
    ImageProcessingEntity,
)
from homeassistant.const import (
    HTTP_BAD_REQUEST,
    HTTP_OK,
    HTTP_UNAUTHORIZED,
)
from homeassistant.core import split_entity_id
from homeassistant.helpers import template
import homeassistant.helpers.config_validation as cv

_LOGGER = logging.getLogger(__name__)

ATTR_MATCHES = "matches"
ATTR_SUMMARY = "summary"
ATTR_TOTAL_MATCHES = "total_matches"

CONF_URL = "url"
CONF_AUTH_KEY = "auth_key"
CONF_DETECTOR = "detector"
CONF_LABELS = "labels"
CONF_AREA = "area"
CONF_TOP = "top"
CONF_BOTTOM = "bottom"
CONF_RIGHT = "right"
CONF_LEFT = "left"
CONF_FILE_OUT = "file_out"

AREA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_BOTTOM, default=1): cv.small_float,
        vol.Optional(CONF_LEFT, default=0): cv.small_float,
        vol.Optional(CONF_RIGHT, default=1): cv.small_float,
        vol.Optional(CONF_TOP, default=0): cv.small_float,
    }
)

LABEL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_AREA): AREA_SCHEMA,
        vol.Optional(CONF_CONFIDENCE): vol.Range(min=0, max=100),
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_URL): cv.string,
        vol.Required(CONF_DETECTOR): cv.string,
        vol.Optional(CONF_AUTH_KEY, default=""): cv.string,
        vol.Optional(CONF_FILE_OUT, default=[]): vol.All(cv.ensure_list, [cv.template]),
        vol.Optional(CONF_CONFIDENCE, default=0.0): vol.Range(min=0, max=100),
        vol.Optional(CONF_LABELS, default=[]): vol.All(
            cv.ensure_list, [vol.Any(cv.string, LABEL_SCHEMA)]
        ),
        vol.Optional(CONF_AREA): AREA_SCHEMA,
    }
)


def get_detectors(url, auth_key):
    """Check the health and return its id if healthy."""
    kwargs = {}
    if auth_key:
        kwargs['headers'] = {'doods-auth-key': auth_key}
    try:
        response = requests.get(
            url + "/detectors",
            **kwargs
        )
        if response.status_code == HTTP_UNAUTHORIZED:
            _LOGGER.error("AuthenticationError on %s", CLASSIFIER)
            return None
        if response.status_code == HTTP_OK:
            return response.json()
    except requests.exceptions.ConnectionError:
        _LOGGER.error("ConnectionError: Is %s running?", CLASSIFIER)
        return None


def detect(url, auth_key, image, dconfig):
    """Post an image to the detector."""
    kwargs = {}
    if auth_key:
        kwargs['headers'] = {'doods-auth-key': auth_key}
    try:
        response = requests.post(
            url + "/detect",
            json={"data": encode_image(image), "detect": dconfig},
            **kwargs
        )
        if response.status_code == HTTP_UNAUTHORIZED:
            _LOGGER.error("AuthenticationError on %s", CLASSIFIER)
            return None
        if response.status_code == HTTP_OK:
            return response.json()
    except requests.exceptions.ConnectionError:
        _LOGGER.error("ConnectionError: Is %s running?", CLASSIFIER)
        return None


def encode_image(image):
    """base64 encode an image stream."""
    base64_img = base64.b64encode(image).decode('ascii')
    return base64_img


def draw_box(draw, box, img_width, img_height, text="", color=(255, 255, 0)):
    """Draw bounding box on image."""
    ymin, xmin, ymax, xmax = box
    (left, right, top, bottom) = (
        xmin * img_width,
        xmax * img_width,
        ymin * img_height,
        ymax * img_height,
    )
    draw.line(
        [(left, top), (left, bottom), (right, bottom), (right, top), (left, top)],
        width=5,
        fill=color,
    )
    if text:
        draw.text((left, abs(top - 15)), text, fill=color)


def setup_platform(hass, config, add_entities, discovery_info=None):
    """Set up the Doods client."""
    url = config[CONF_URL]
    auth_key = config[CONF_AUTH_KEY]
    detector_name = config[CONF_DETECTOR]
    response = get_detectors(url, auth_key)
    if not isinstance(response, dict):
        _LOGGER.warning("Could not connect to doods server: %s", url)
        return

    detector = {}
    for d in response["detectors"]:
        if d["name"] == detector_name:
            detector = d
            break

    if not detector:
        _LOGGER.warning(
            "Detector %s is not supported by doods server %s", detector_name, url)
        return

    entities = []
    for camera in config[CONF_SOURCE]:
        entities.append(
            Doods(
                hass,
                camera[CONF_ENTITY_ID],
                camera.get(CONF_NAME),
                url,
                auth_key,
                detector,
                config,
            )
        )
    add_entities(entities)


class Doods(ImageProcessingEntity):
    """Doods image processing service client."""

    def __init__(
        self,
        hass,
        camera_entity,
        name,
        url,
        auth_key,
        detector,
        config,
    ):
        """Initialize the DOODS entity."""
        self.hass = hass
        self._camera_entity = camera_entity
        if name:
            self._name = name
        else:
            self._name = "Doods {0}".format(split_entity_id(camera_entity)[1])
        self._url = url
        self._auth_key = auth_key
        self._file_out = config.get(CONF_FILE_OUT)

        # detector config and aspect ratio
        if detector["width"] and detector["height"]:
            self._width = detector["width"]
            self._height = detector["height"]
            self._aspect = self._width / self._height

        # the base confidence
        dconfig = {}
        confidence = config.get(CONF_CONFIDENCE)
        if not confidence:
            confidence = 0

        # handle labels and specific detection areas
        labels = config.get(CONF_LABELS)
        self._label_areas = {}
        for label in labels:
            if isinstance(label, dict):
                label_name = label.get(CONF_NAME)
                if label_name not in detector["labels"] and label_name != "*":
                    _LOGGER.warning(
                        "Detector does not support label %s", label_name)
                    continue

                # Label Confidence
                label_confidence = label.get(CONF_CONFIDENCE)
                if not label_confidence:
                    label_confidence = confidence
                if label_name not in dconfig or dconfig[label_name] > label_confidence:
                    dconfig[label_name] = label_confidence

                # Label area
                label_area = label.get(CONF_AREA)
                self._label_areas[label_name] = [0, 0, 1, 1]
                if label_area:
                    self._label_areas[label_name] = [
                        label_area.get(CONF_TOP),
                        label_area.get(CONF_LEFT),
                        label_area.get(CONF_BOTTOM),
                        label_area.get(CONF_RIGHT),
                    ]
            else:
                if label not in detector["labels"] and label != "*":
                    _LOGGER.warning(
                        "Detector does not support label %s", label)
                    continue
                self._label_areas[label] = [0, 0, 1, 1]
                if label not in dconfig or dconfig[label] > confidence:
                    dconfig[label] = confidence

        if not dconfig:
            dconfig["*"] = confidence

        # Handle global detection area
        self._area = [0, 0, 1, 1]
        area_config = config.get(CONF_AREA)
        if area_config:
            self._area = [
                area_config.get(CONF_TOP),
                area_config.get(CONF_LEFT),
                area_config.get(CONF_BOTTOM),
                area_config.get(CONF_RIGHT),
            ]

        template.attach(hass, self._file_out)

        self._dconfig = dconfig
        self._matches = {}
        self._total_matches = 0
        self._last_image = None

    @property
    def camera_entity(self):
        """Return camera entity id from process pictures."""
        return self._camera_entity

    @property
    def name(self):
        """Return the name of the image processor."""
        return self._name

    @property
    def state(self):
        """Return the state of the entity."""
        return self._total_matches

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        return {
            ATTR_MATCHES: self._matches,
            ATTR_SUMMARY: {
                label: len(values) for label, values in self._matches.items()
            },
            ATTR_TOTAL_MATCHES: self._total_matches,
        }

    def _save_image(self, image, matches, paths):
        from PIL import Image, ImageDraw
        import io

        img = Image.open(io.BytesIO(bytearray(image))).convert("RGB")
        img_width, img_height = img.size
        draw = ImageDraw.Draw(img)

        # Draw custom global region/area
        if self._area != [0, 0, 1, 1]:
            draw_box(
                draw, self._area, img_width, img_height, "Detection Area", (
                    0, 255, 255)
            )

        for label, values in matches.items():

            # Draw custom label regions/areas
            if label in self._label_areas and self._label_areas[label] != [
                0,
                0,
                1,
                1,
            ]:
                box_label = "{} Detection Area".format(label.capitalize())
                draw_box(
                    draw,
                    self._label_areas[label],
                    img_width,
                    img_height,
                    box_label,
                    (0, 255, 0),
                )

            # Draw detected objects
            for instance in values:
                box_label = "{0} {1:.1f}%".format(label, instance["score"])
                # Already scaled, use 1 for width and height
                draw_box(draw, instance["box"], img_width,
                         img_height, box_label, (255, 255, 0))

        for path in paths:
            _LOGGER.info("Saving results image to %s", path)
            img.save(path)

    def process_image(self, image):
        """Process the image."""

        from PIL import Image
        import io
        img = Image.open(io.BytesIO(bytearray(image)))
        img_width, img_height = img.size

        if self._aspect and abs((img_width/img_height) - self._aspect) > 0.1:
            _LOGGER.warn("The image aspect: %s and the detector aspect: %s differ by more than 0.1",
                         (img_width/img_height), self._aspect)

        # Run detection
        start = time.time()
        dconfig = {}
        response = detect(self._url, self._auth_key, image, self._dconfig)
        _LOGGER.info("doods detect: %s response: %s duration: %s",
                     self._dconfig, response, time.time()-start)

        matches = {}
        total_matches = 0

        if response:
            # Was there an error
            if "error" in response:
                _LOGGER.error(response["error"])
            else:
                for d in response["detections"]:
                    score = d["confidence"]
                    boxes = [d["top"], d["left"], d["bottom"], d["right"]]
                    label = d["label"]

                    # Exclude unlisted labels
                    if "*" not in self._dconfig and label not in self._dconfig:
                        continue

                    # Exclude matches outside global area definition
                    if (
                        boxes[0] < self._area[0] * img_height
                        or boxes[1] < self._area[1] * img_width
                        or boxes[2] > self._area[2] * img_height
                        or boxes[3] > self._area[3] * img_width
                    ):
                        continue

                    # Exclude matches outside label specific area definition
                    if self._label_areas and (
                        boxes[0] < self._label_areas[label][0] * img_height
                        or boxes[1] < self._label_areas[label][1] * img_width
                        or boxes[2] > self._label_areas[label][2] * img_height
                        or boxes[3] > self._label_areas[label][3] * img_width
                    ):
                        continue

                    if label not in matches.keys():
                        matches[label] = []
                    matches[label].append(
                        {"score": float(score), "box": boxes})
                    total_matches += 1

                    # Save Images
                    if total_matches and self._file_out:
                        paths = []
                        for path_template in self._file_out:
                            if isinstance(path_template, template.Template):
                                paths.append(
                                    path_template.render(
                                        camera_entity=self._camera_entity)
                                )
                            else:
                                paths.append(path_template)
                        self._save_image(image, matches, paths)

        self._matches = matches
        self._total_matches = total_matches
