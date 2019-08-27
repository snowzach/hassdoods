# DOODS
Dedicated Open Object Detection Service - Yes, it's a backronym, so what...

DOODS is a GRPC service that detects objects in images. It's designed to be run as a container, optionally remotely. 

# HASS
This is a hass component that follows the `image_processing` component type.

It loosely follows the tensorflow configuration.

```
image_processing:
  - platform: doods
    scan_interval: 1000
    url: "http://<my docker host>:8080"
    detector: default
    file_out:
      - "/tmp/{{ camera_entity.split('.')[1] }}_latest.jpg"
    source:
      - entity_id: camera.front_yard
    confidence: 50
    labels:
      - name: person
        confidence: 40
        area:
          # Exclude top 10% of image
          top: 0.1
          # Exclude right 15% of image
          right: 0.85
      - car
      - truck
```

