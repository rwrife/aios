// Private, fixed-format V4L2 adapter. Called only inside the disposable worker.
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <poll.h>
#include <unistd.h>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <new>

namespace {
struct Camera {
    int fd = -1;
    void *buffers[4]{};
    size_t lengths[4]{};
    bool streaming = false;
    ~Camera() {
        if (streaming) { int type = V4L2_BUF_TYPE_VIDEO_CAPTURE; ioctl(fd, VIDIOC_STREAMOFF, &type); }
        for (unsigned i = 0; i < 4; ++i)
            if (buffers[i]) munmap(buffers[i], lengths[i]);
        if (fd >= 0) close(fd);
    }
};
int control(int fd, unsigned long request, void *arg) {
    int result;
    do { result = ioctl(fd, request, arg); } while (result < 0 && errno == EINTR);
    return result;
}
}

extern "C" void *aios_camera_open(const char *path) {
    Camera *camera = new (std::nothrow) Camera;
    if (!camera) return nullptr;
    camera->fd = open(path, O_RDWR | O_NONBLOCK | O_CLOEXEC);
    auto fail = [&]() -> void * { delete camera; return nullptr; };
    if (camera->fd < 0) return fail();
    v4l2_capability capability{};
    if (control(camera->fd, VIDIOC_QUERYCAP, &capability) < 0) return fail();
    uint32_t caps = capability.capabilities & V4L2_CAP_DEVICE_CAPS ? capability.device_caps : capability.capabilities;
    if (!(caps & V4L2_CAP_VIDEO_CAPTURE) || !(caps & V4L2_CAP_STREAMING)) return fail();
    v4l2_format format{};
    format.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    format.fmt.pix.width = 640;
    format.fmt.pix.height = 480;
    format.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
    format.fmt.pix.field = V4L2_FIELD_ANY;
    if (control(camera->fd, VIDIOC_S_FMT, &format) < 0 || format.fmt.pix.width != 640 ||
        format.fmt.pix.height != 480 || format.fmt.pix.pixelformat != V4L2_PIX_FMT_MJPEG) return fail();
    v4l2_streamparm parameters{};
    parameters.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    parameters.parm.capture.timeperframe.numerator = 1;
    parameters.parm.capture.timeperframe.denominator = 15;
    if (control(camera->fd, VIDIOC_S_PARM, &parameters) < 0) return fail();
    v4l2_requestbuffers request{};
    request.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    request.memory = V4L2_MEMORY_MMAP;
    request.count = 1;
    if (control(camera->fd, VIDIOC_REQBUFS, &request) < 0 || request.count < 1 || request.count > 4) return fail();
    for (unsigned i = 0; i < request.count; ++i) {
        v4l2_buffer buffer{};
        buffer.type = request.type;
        buffer.memory = request.memory;
        buffer.index = i;
        if (control(camera->fd, VIDIOC_QUERYBUF, &buffer) < 0 || !buffer.length || buffer.length > 1048576) return fail();
        void *mapped = mmap(nullptr, buffer.length, PROT_READ | PROT_WRITE, MAP_SHARED, camera->fd, buffer.m.offset);
        if (mapped == MAP_FAILED) return fail();
        camera->buffers[i] = mapped;
        camera->lengths[i] = buffer.length;
        if (control(camera->fd, VIDIOC_QBUF, &buffer) < 0) return fail();
    }
    int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (control(camera->fd, VIDIOC_STREAMON, &type) < 0) return fail();
    camera->streaming = true;
    return camera;
}

// Returns JPEG byte count, or a fixed negative diagnostic code. Timestamp is the driver's monotonic capture
// timestamp, never the time read() completed. Unsupported clocks fail closed.
extern "C" int aios_camera_read(void *handle, unsigned char *output, size_t capacity,
                                double *timestamp, uint32_t *sequence) {
    auto *camera = static_cast<Camera *>(handle);
    if (!camera || !output || !timestamp || !sequence) return -1;
    pollfd descriptor{camera->fd, POLLIN, 0};
    const int ready = poll(&descriptor, 1, 1500);
    if (ready == 0) return -2;
    if (ready < 0 || !(descriptor.revents & POLLIN)) return -3;
    v4l2_buffer buffer{};
    buffer.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buffer.memory = V4L2_MEMORY_MMAP;
    if (control(camera->fd, VIDIOC_DQBUF, &buffer) < 0) return -4;
    const bool safe = buffer.index < 4 && camera->buffers[buffer.index] &&
        buffer.bytesused <= capacity && buffer.bytesused <= camera->lengths[buffer.index] &&
        (buffer.flags & V4L2_BUF_FLAG_TIMESTAMP_MASK) == V4L2_BUF_FLAG_TIMESTAMP_MONOTONIC &&
        buffer.timestamp.tv_sec >= 0 && buffer.timestamp.tv_usec >= 0 && buffer.timestamp.tv_usec < 1000000;
    const bool valid = safe && buffer.bytesused > 0 && !(buffer.flags & V4L2_BUF_FLAG_ERROR);
    if (valid) {
        std::memcpy(output, camera->buffers[buffer.index], buffer.bytesused);
        *timestamp = double(buffer.timestamp.tv_sec) + double(buffer.timestamp.tv_usec) / 1000000;
        *sequence = buffer.sequence;
    }
    const int count = valid ? int(buffer.bytesused) :
        (!safe ? -5 : ((buffer.flags & V4L2_BUF_FLAG_ERROR) ? -7 : -8));
    if (control(camera->fd, VIDIOC_QBUF, &buffer) < 0) return -6;
    return count;
}

extern "C" void aios_camera_close(void *handle) { delete static_cast<Camera *>(handle); }
