/* SPDX-License-Identifier: GPL-3.0-or-later */
/* wlr virtual-pointer client. Uses only the WAYLAND_DISPLAY supplied by bgui.py. */
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <wayland-client.h>
#include "virtual-pointer-client-protocol.h"

static struct zwlr_virtual_pointer_manager_v1 *manager;
static struct wl_seat *seat;
static void global(void *data, struct wl_registry *registry, uint32_t name,
                   const char *interface, uint32_t version) {
    (void)data; (void)version;
    if (!strcmp(interface, "zwlr_virtual_pointer_manager_v1"))
        manager = wl_registry_bind(registry, name, &zwlr_virtual_pointer_manager_v1_interface, 1);
    else if (!strcmp(interface, "wl_seat"))
        seat = wl_registry_bind(registry, name, &wl_seat_interface, 1);
}
static void removed(void *data, struct wl_registry *registry, uint32_t name) {
    (void)data; (void)registry; (void)name;
}
static const struct wl_registry_listener listener = {global, removed};
static uint32_t now_ms(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint32_t)(t.tv_sec * 1000 + t.tv_nsec / 1000000);
}
static void pause_event(void) {
    struct timespec t = {0, 60000000}; nanosleep(&t, NULL);
}
static int sync_display(struct wl_display *display) {
    if (wl_display_roundtrip(display) < 0) {
        fprintf(stderr, "Wayland connection failed\n"); return -1;
    }
    return 0;
}
int main(int argc, char **argv) {
    if (argc < 2) return 2;
    const char *mode = argv[1];
    if ((!strcmp(mode, "move") && argc != 6) ||
        (!strcmp(mode, "click") && argc != 7) ||
        (!strcmp(mode, "scroll") && argc != 4)) return 2;
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) { perror("wl_display_connect"); return 3; }
    struct wl_registry *registry = wl_display_get_registry(display);
    wl_registry_add_listener(registry, &listener, NULL);
    if (sync_display(display) < 0) return 3;
    if (!manager) { fprintf(stderr, "wlr virtual pointer is unavailable\n"); return 4; }
    struct zwlr_virtual_pointer_v1 *pointer =
        zwlr_virtual_pointer_manager_v1_create_virtual_pointer(manager, seat);
    if (!strcmp(mode, "move") || !strcmp(mode, "click")) {
        zwlr_virtual_pointer_v1_motion_absolute(pointer, now_ms(),
            (uint32_t)strtoul(argv[2], NULL, 10), (uint32_t)strtoul(argv[3], NULL, 10),
            (uint32_t)strtoul(argv[4], NULL, 10), (uint32_t)strtoul(argv[5], NULL, 10));
        zwlr_virtual_pointer_v1_frame(pointer);
        if (sync_display(display) < 0) return 3;
        pause_event();
        if (!strcmp(mode, "click")) {
            uint32_t button = (uint32_t)strtoul(argv[6], NULL, 10);
            zwlr_virtual_pointer_v1_button(pointer, now_ms(), button, WL_POINTER_BUTTON_STATE_PRESSED);
            zwlr_virtual_pointer_v1_frame(pointer);
            if (sync_display(display) < 0) return 3;
            pause_event();
            zwlr_virtual_pointer_v1_button(pointer, now_ms(), button, WL_POINTER_BUTTON_STATE_RELEASED);
            zwlr_virtual_pointer_v1_frame(pointer);
        }
    } else if (!strcmp(mode, "scroll")) {
        double vertical = strtod(argv[2], NULL), horizontal = strtod(argv[3], NULL);
        if (!isfinite(vertical) || !isfinite(horizontal)) return 2;
        zwlr_virtual_pointer_v1_axis_source(pointer, WL_POINTER_AXIS_SOURCE_WHEEL);
        if (vertical != 0)
            zwlr_virtual_pointer_v1_axis(pointer, now_ms(), WL_POINTER_AXIS_VERTICAL_SCROLL,
                                         wl_fixed_from_double(vertical));
        if (horizontal != 0)
            zwlr_virtual_pointer_v1_axis(pointer, now_ms(), WL_POINTER_AXIS_HORIZONTAL_SCROLL,
                                         wl_fixed_from_double(horizontal));
        zwlr_virtual_pointer_v1_frame(pointer);
    } else { fprintf(stderr, "Unknown pointer operation\n"); return 2; }
    if (sync_display(display) < 0) return 3;
    pause_event();
    zwlr_virtual_pointer_v1_destroy(pointer);
    wl_display_flush(display);
    wl_display_disconnect(display);
    return 0;
}
