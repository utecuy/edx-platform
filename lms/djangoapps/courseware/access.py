"""
This file contains (or should), all access control logic for the courseware.
Ideally, it will be the only place that needs to know about any special settings
like DISABLE_START_DATES.

Note: The access control logic in this file does NOT check for enrollment in
  a course.  It is expected that higher layers check for enrollment so we
  don't have to hit the enrollments table on every module load.

  If enrollment is to be checked, use get_course_with_access in courseware.courses.
  It is a wrapper around has_access that additionally checks for enrollment.
"""
import logging
from datetime import datetime, timedelta
import pytz

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.utils.timezone import UTC

from opaque_keys.edx.keys import CourseKey, UsageKey

from xblock.core import XBlock

from xmodule.course_module import (
    CourseDescriptor, CATALOG_VISIBILITY_CATALOG_AND_ABOUT,
    CATALOG_VISIBILITY_ABOUT)
from xmodule.error_module import ErrorDescriptor
from xmodule.x_module import XModule, DEPRECATION_VSCOMPAT_EVENT
from xmodule.split_test_module import get_split_user_partitions
from xmodule.partitions.partitions import NoSuchUserPartitionError, NoSuchUserPartitionGroupError
from xmodule.util.django import get_current_request_hostname

from external_auth.models import ExternalAuthMap
from courseware.masquerade import get_masquerade_role, is_masquerading_as_student
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from student import auth
from student.models import CourseEnrollmentAllowed
from student.roles import (
    GlobalStaff, CourseStaffRole, CourseInstructorRole,
    OrgStaffRole, OrgInstructorRole, CourseBetaTesterRole
)
from util.milestones_helpers import (
    get_pre_requisite_courses_not_completed,
    any_unfulfilled_milestones,
)
from ccx_keys.locator import CCXLocator

import dogstats_wrapper as dog_stats_api

DEBUG_ACCESS = False

log = logging.getLogger(__name__)


def debug(*args, **kwargs):
    # to avoid overly verbose output, this is off by default
    if DEBUG_ACCESS:
        log.debug(*args, **kwargs)


def has_access(user, action, obj, course_key=None):
    """
    Check whether a user has the access to do action on obj.  Handles any magic
    switching based on various settings.

    Things this module understands:
    - start dates for modules
    - visible_to_staff_only for modules
    - DISABLE_START_DATES
    - different access for instructor, staff, course staff, and students.
    - mobile_available flag for course modules

    user: a Django user object. May be anonymous. If none is passed,
                    anonymous is assumed

    obj: The object to check access for.  A module, descriptor, location, or
                    certain special strings (e.g. 'global')

    action: A string specifying the action that the client is trying to perform.

    actions depend on the obj type, but include e.g. 'enroll' for courses.  See the
    type-specific functions below for the known actions for that type.

    course_key: A course_key specifying which course run this access is for.
        Required when accessing anything other than a CourseDescriptor, 'global',
        or a location with category 'course'

    Returns a bool.  It is up to the caller to actually deny access in a way
    that makes sense in context.
    """
    # Just in case user is passed in as None, make them anonymous
    if not user:
        user = AnonymousUser()

    if isinstance(course_key, CCXLocator):
        course_key = course_key.to_course_locator()

    # delegate the work to type-specific functions.
    # (start with more specific types, then get more general)
    if isinstance(obj, CourseDescriptor):
        return _has_access_course_desc(user, action, obj)

    if isinstance(obj, CourseOverview):
        return _has_access_course_overview(user, action, obj)

    if isinstance(obj, ErrorDescriptor):
        return _has_access_error_desc(user, action, obj, course_key)

    if isinstance(obj, XModule):
        return _has_access_xmodule(user, action, obj, course_key)

    # NOTE: any descriptor access checkers need to go above this
    if isinstance(obj, XBlock):
        return _has_access_descriptor(user, action, obj, course_key)

    if isinstance(obj, CCXLocator):
        return _has_access_ccx_key(user, action, obj)

    if isinstance(obj, CourseKey):
        return _has_access_course_key(user, action, obj)

    if isinstance(obj, UsageKey):
        return _has_access_location(user, action, obj, course_key)

    if isinstance(obj, basestring):
        return _has_access_string(user, action, obj)

    # Passing an unknown object here is a coding error, so rather than
    # returning a default, complain.
    raise TypeError("Unknown object type in has_access(): '{0}'"
                    .format(type(obj)))


# ================ Implementation helpers ================================
def _can_access_descriptor_with_start_date(user, descriptor, course_key):  # pylint: disable=invalid-name
    """
    Checks if a user has access to a descriptor based on its start date.

    If there is no start date specified, grant access.
    Else, check if we're past the start date.

    Note:
        We do NOT check whether the user is staff or if the descriptor
        is detached... it is assumed both of these are checked by the caller.

    Arguments:
        user (User): the user whose descriptor access we are checking.
        descriptor (AType): the descriptor for which we are checking access.
    where AType is any descriptor that has the attributes .location and
        .days_early_for_beta
    """
    start_dates_disabled = settings.FEATURES['DISABLE_START_DATES']
    if start_dates_disabled and not is_masquerading_as_student(user, course_key):
        return True
    else:
        now = datetime.now(UTC())
        effective_start = _adjust_start_date_for_beta_testers(
            user,
            descriptor,
            course_key=course_key
        )
        return (
            descriptor.start is None
            or now > effective_start
            or in_preview_mode()
        )


def _can_view_courseware_with_prerequisites(user, course):  # pylint: disable=invalid-name
    """
    Checks if a user has access to a course based on its prerequisites.

    If the user is staff or anonymous, immediately grant access.
    Else, return whether or not the prerequisite courses have been passed.

    Arguments:
        user (User): the user whose course access we are checking.
        course (AType): the course for which we are checking access.
    where AType is CourseDescriptor, CourseOverview, or any other class that
        represents a course and has the attributes .location and .id.
    """
    return (
        not settings.FEATURES['ENABLE_PREREQUISITE_COURSES']
        or _has_staff_access_to_descriptor(user, course, course.id)
        or not course.pre_requisite_courses
        or user.is_anonymous()
        or not get_pre_requisite_courses_not_completed(user, [course.id])
    )


def _can_load_course_on_mobile(user, course):
    """
    Checks if a user can view the given course on a mobile device.

    This function only checks mobile-specific access restrictions. Other access
    restrictions such as start date and the .visible_to_staff_only flag must
    be checked by callers in *addition* to the return value of this function.

    Arguments:
        user (User): the user whose course access  we are checking.
        course (CourseDescriptor|CourseOverview): the course for which we are
            checking access.

    Returns:
        bool: whether the course can be accessed on mobile.
    """
    return (
        is_mobile_available_for_user(user, course) and
        (
            _has_staff_access_to_descriptor(user, course, course.id) or
            not any_unfulfilled_milestones(course.id, user.id)
        )
    )


def _has_access_course_desc(user, action, course):
    """
    Check if user has access to a course descriptor.

    Valid actions:

    'load' -- load the courseware, see inside the course
    'load_forum' -- can load and contribute to the forums (one access level for now)
    'load_mobile' -- can load from a mobile context
    'enroll' -- enroll.  Checks for enrollment window,
                  ACCESS_REQUIRE_STAFF_FOR_COURSE,
    'see_exists' -- can see that the course exists.
    'staff' -- staff access to course.
    'see_in_catalog' -- user is able to see the course listed in the course catalog.
    'see_about_page' -- user is able to see the course about page.
    """
    def can_load():
        """
        Can this user load this course?

        NOTE: this is not checking whether user is actually enrolled in the course.
        """
        # delegate to generic descriptor check to check start dates
        return _has_access_descriptor(user, 'load', course, course.id)

    def can_enroll():
        """
        First check if restriction of enrollment by login method is enabled, both
            globally and by the course.
        If it is, then the user must pass the criterion set by the course, e.g. that ExternalAuthMap
            was set by 'shib:https://idp.stanford.edu/", in addition to requirements below.
        Rest of requirements:
        (CourseEnrollmentAllowed always overrides)
          or
        (staff can always enroll)
          or
        Enrollment can only happen in the course enrollment period, if one exists, and
        course is not invitation only.
        """

        # if using registration method to restrict (say shibboleth)
        if settings.FEATURES.get('RESTRICT_ENROLL_BY_REG_METHOD') and course.enrollment_domain:
            if user is not None and user.is_authenticated() and \
                    ExternalAuthMap.objects.filter(user=user, external_domain=course.enrollment_domain):
                debug("Allow: external_auth of " + course.enrollment_domain)
                reg_method_ok = True
            else:
                reg_method_ok = False
        else:
            reg_method_ok = True  # if not using this access check, it's always OK.

        now = datetime.now(UTC())
        start = course.enrollment_start or datetime.min.replace(tzinfo=pytz.UTC)
        end = course.enrollment_end or datetime.max.replace(tzinfo=pytz.UTC)

        # if user is in CourseEnrollmentAllowed with right course key then can also enroll
        # (note that course.id actually points to a CourseKey)
        # (the filter call uses course_id= since that's the legacy database schema)
        # (sorry that it's confusing :( )
        if user is not None and user.is_authenticated() and CourseEnrollmentAllowed:
            if CourseEnrollmentAllowed.objects.filter(email=user.email, course_id=course.id):
                return True

        if _has_staff_access_to_descriptor(user, course, course.id):
            return True

        # Invitation_only doesn't apply to CourseEnrollmentAllowed or has_staff_access_access
        if course.invitation_only:
            debug("Deny: invitation only")
            return False

        if reg_method_ok and start < now < end:
            debug("Allow: in enrollment period")
            return True

    def see_exists():
        """
        Can see if can enroll, but also if can load it: if user enrolled in a course and now
        it's past the enrollment period, they should still see it.

        TODO (vshnayder): This means that courses with limited enrollment periods will not appear
        to non-staff visitors after the enrollment period is over.  If this is not what we want, will
        need to change this logic.
        """
        # VS[compat] -- this setting should go away once all courses have
        # properly configured enrollment_start times (if course should be
        # staff-only, set enrollment_start far in the future.)
        if settings.FEATURES.get('ACCESS_REQUIRE_STAFF_FOR_COURSE'):
            dog_stats_api.increment(
                DEPRECATION_VSCOMPAT_EVENT,
                tags=(
                    "location:has_access_course_desc_see_exists",
                    u"course:{}".format(course),
                )
            )

            # if this feature is on, only allow courses that have ispublic set to be
            # seen by non-staff
            if course.ispublic:
                debug("Allow: ACCESS_REQUIRE_STAFF_FOR_COURSE and ispublic")
                return True
            return _has_staff_access_to_descriptor(user, course, course.id)

        return can_enroll() or can_load()

    def can_see_in_catalog():
        """
        Implements the "can see course in catalog" logic if a course should be visible in the main course catalog
        In this case we use the catalog_visibility property on the course descriptor
        but also allow course staff to see this.
        """
        return (
            course.catalog_visibility == CATALOG_VISIBILITY_CATALOG_AND_ABOUT or
            _has_staff_access_to_descriptor(user, course, course.id)
        )

    def can_see_about_page():
        """
        Implements the "can see course about page" logic if a course about page should be visible
        In this case we use the catalog_visibility property on the course descriptor
        but also allow course staff to see this.
        """
        return (
            course.catalog_visibility == CATALOG_VISIBILITY_CATALOG_AND_ABOUT or
            course.catalog_visibility == CATALOG_VISIBILITY_ABOUT or
            _has_staff_access_to_descriptor(user, course, course.id)
        )

    checkers = {
        'load': can_load,
        'view_courseware_with_prerequisites':
            lambda: _can_view_courseware_with_prerequisites(user, course),
        'load_mobile': lambda: can_load() and _can_load_course_on_mobile(user, course),
        'enroll': can_enroll,
        'see_exists': see_exists,
        'staff': lambda: _has_staff_access_to_descriptor(user, course, course.id),
        'instructor': lambda: _has_instructor_access_to_descriptor(user, course, course.id),
        'see_in_catalog': can_see_in_catalog,
        'see_about_page': can_see_about_page,
    }

    return _dispatch(checkers, action, user, course)


def _can_load_course_overview(user, course_overview):
    """
    Check if a user can load a course overview.

    Arguments:
        user (User): the user whose course access we are checking.
        course_overview (CourseOverview): a course overview.

    Note:
        The user doesn't have to be enrolled in the course in order to have load
        load access.
    """
    return (
        not course_overview.visible_to_staff_only
        and _can_access_descriptor_with_start_date(user, course_overview, course_overview.id)
    ) or _has_staff_access_to_descriptor(user, course_overview, course_overview.id)


_COURSE_OVERVIEW_CHECKERS = {
    'load': _can_load_course_overview,
    'load_mobile': lambda user, course_overview: (
        _can_load_course_overview(user, course_overview)
        and _can_load_course_on_mobile(user, course_overview)
    ),
    'view_courseware_with_prerequisites': _can_view_courseware_with_prerequisites
}
COURSE_OVERVIEW_SUPPORTED_ACTIONS = _COURSE_OVERVIEW_CHECKERS.keys()  # pylint: disable=invalid-name


def _has_access_course_overview(user, action, course_overview):
    """
    Check if user has access to a course overview.

    Arguments:
        user (User): the user whose course access we are checking.
        action (str): the action the user is trying to perform.
            See COURSE_OVERVIEW_SUPPORTED_ACTIONS for valid values.
        course_overview (CourseOverview): overview of the course in question.
    """
    if action in _COURSE_OVERVIEW_CHECKERS:
        return _COURSE_OVERVIEW_CHECKERS[action](user, course_overview)
    else:
        raise ValueError(u"Unknown action for object type 'CourseOverview': '{1}'".format(action))


def _has_access_error_desc(user, action, descriptor, course_key):
    """
    Only staff should see error descriptors.

    Valid actions:
    'load' -- load this descriptor, showing it to the user.
    'staff' -- staff access to descriptor.
    """
    def check_for_staff():
        return _has_staff_access_to_descriptor(user, descriptor, course_key)

    checkers = {
        'load': check_for_staff,
        'staff': check_for_staff,
        'instructor': lambda: _has_instructor_access_to_descriptor(user, descriptor, course_key)
    }

    return _dispatch(checkers, action, user, descriptor)


def _has_group_access(descriptor, user, course_key):
    """
    This function returns a boolean indicating whether or not `user` has
    sufficient group memberships to "load" a block (the `descriptor`)
    """
    if len(descriptor.user_partitions) == len(get_split_user_partitions(descriptor.user_partitions)):
        # Short-circuit the process, since there are no defined user partitions that are not
        # user_partitions used by the split_test module. The split_test module handles its own access
        # via updating the children of the split_test module.
        return True

    # use merged_group_access which takes group access on the block's
    # parents / ancestors into account
    merged_access = descriptor.merged_group_access
    # check for False in merged_access, which indicates that at least one
    # partition's group list excludes all students.
    if False in merged_access.values():
        log.warning("Group access check excludes all students, access will be denied.", exc_info=True)
        return False

    # resolve the partition IDs in group_access to actual
    # partition objects, skipping those which contain empty group directives.
    # if a referenced partition could not be found, access will be denied.
    try:
        partitions = [
            descriptor._get_user_partition(partition_id)  # pylint:disable=protected-access
            for partition_id, group_ids in merged_access.items()
            if group_ids is not None
        ]
    except NoSuchUserPartitionError:
        log.warning("Error looking up user partition, access will be denied.", exc_info=True)
        return False

    # next resolve the group IDs specified within each partition
    partition_groups = []
    try:
        for partition in partitions:
            groups = [
                partition.get_group(group_id)
                for group_id in merged_access[partition.id]
            ]
            if groups:
                partition_groups.append((partition, groups))
    except NoSuchUserPartitionGroupError:
        log.warning("Error looking up referenced user partition group, access will be denied.", exc_info=True)
        return False

    # look up the user's group for each partition
    user_groups = {}
    for partition, groups in partition_groups:
        user_groups[partition.id] = partition.scheme.get_group_for_user(
            course_key,
            user,
            partition,
        )

    # finally: check that the user has a satisfactory group assignment
    # for each partition.
    if not all(user_groups.get(partition.id) in groups for partition, groups in partition_groups):
        return False

    # all checks passed.
    return True


def _has_access_descriptor(user, action, descriptor, course_key=None):
    """
    Check if user has access to this descriptor.

    Valid actions:
    'load' -- load this descriptor, showing it to the user.
    'staff' -- staff access to descriptor.

    NOTE: This is the fallback logic for descriptors that don't have custom policy
    (e.g. courses).  If you call this method directly instead of going through
    has_access(), it will not do the right thing.
    """
    def can_load():
        """
        NOTE: This does not check that the student is enrolled in the course
        that contains this module.  We may or may not want to allow non-enrolled
        students to see modules.  If not, views should check the course, so we
        don't have to hit the enrollments table on every module load.
        """
        return (
            not descriptor.visible_to_staff_only
            and _has_group_access(descriptor, user, course_key)
            and (
                'detached' in descriptor._class_tags  # pylint: disable=protected-access
                or _can_access_descriptor_with_start_date(user, descriptor, course_key)
            )
        ) or _has_staff_access_to_descriptor(user, descriptor, course_key)

    checkers = {
        'load': can_load,
        'staff': lambda: _has_staff_access_to_descriptor(user, descriptor, course_key),
        'instructor': lambda: _has_instructor_access_to_descriptor(user, descriptor, course_key)
    }

    return _dispatch(checkers, action, user, descriptor)


def _has_access_xmodule(user, action, xmodule, course_key):
    """
    Check if user has access to this xmodule.

    Valid actions:
      - same as the valid actions for xmodule.descriptor
    """
    # Delegate to the descriptor
    return has_access(user, action, xmodule.descriptor, course_key)


def _has_access_location(user, action, location, course_key):
    """
    Check if user has access to this location.

    Valid actions:
    'staff' : True if the user has staff access to this location

    NOTE: if you add other actions, make sure that

     has_access(user, location, action) == has_access(user, get_item(location), action)
    """
    checkers = {
        'staff': lambda: _has_staff_access_to_location(user, location, course_key)
    }

    return _dispatch(checkers, action, user, location)


def _has_access_course_key(user, action, course_key):
    """
    Check if user has access to the course with this course_key

    Valid actions:
    'staff' : True if the user has staff access to this location
    'instructor' : True if the user has staff access to this location
    """
    checkers = {
        'staff': lambda: _has_staff_access_to_location(user, None, course_key),
        'instructor': lambda: _has_instructor_access_to_location(user, None, course_key),
    }

    return _dispatch(checkers, action, user, course_key)


def _has_access_ccx_key(user, action, ccx_key):
    """Check if user has access to the course for this ccx_key

    Delegates checking to _has_access_course_key
    Valid actions: same as for that function
    """
    course_key = ccx_key.to_course_locator()
    return _has_access_course_key(user, action, course_key)


def _has_access_string(user, action, perm):
    """
    Check if user has certain special access, specified as string.  Valid strings:

    'global'

    Valid actions:

    'staff' -- global staff access.
    """

    def check_staff():
        if perm != 'global':
            debug("Deny: invalid permission '%s'", perm)
            return False
        return GlobalStaff().has_user(user)

    checkers = {
        'staff': check_staff
    }

    return _dispatch(checkers, action, user, perm)


#####  Internal helper methods below

def _dispatch(table, action, user, obj):
    """
    Helper: call table[action], raising a nice pretty error if there is no such key.

    user and object passed in only for error messages and debugging
    """
    if action in table:
        result = table[action]()
        debug("%s user %s, object %s, action %s",
              'ALLOWED' if result else 'DENIED',
              user,
              obj.location.to_deprecated_string() if isinstance(obj, XBlock) else str(obj),
              action)
        return result

    raise ValueError(u"Unknown action for object type '{0}': '{1}'".format(
        type(obj), action))


def _adjust_start_date_for_beta_testers(user, descriptor, course_key=None):  # pylint: disable=invalid-name
    """
    If user is in a beta test group, adjust the start date by the appropriate number of
    days.

    Arguments:
       user: A django user.  May be anonymous.
       descriptor: the XModuleDescriptor the user is trying to get access to, with a
       non-None start date.

    Returns:
        A datetime.  Either the same as start, or earlier for beta testers.

    NOTE: number of days to adjust should be cached to avoid looking it up thousands of
    times per query.

    NOTE: For now, this function assumes that the descriptor's location is in the course
    the user is looking at.  Once we have proper usages and definitions per the XBlock
    design, this should use the course the usage is in.

    NOTE: If testing manually, make sure FEATURES['DISABLE_START_DATES'] = False
    in envs/dev.py!
    """
    if descriptor.days_early_for_beta is None:
        # bail early if no beta testing is set up
        return descriptor.start

    if CourseBetaTesterRole(course_key).has_user(user):
        debug("Adjust start time: user in beta role for %s", descriptor)
        delta = timedelta(descriptor.days_early_for_beta)
        effective = descriptor.start - delta
        return effective

    return descriptor.start


def _has_instructor_access_to_location(user, location, course_key=None):
    if course_key is None:
        course_key = location.course_key
    return _has_access_to_course(user, 'instructor', course_key)


def _has_staff_access_to_location(user, location, course_key=None):
    if course_key is None:
        course_key = location.course_key
    return _has_access_to_course(user, 'staff', course_key)


def _has_access_to_course(user, access_level, course_key):
    '''
    Returns True if the given user has access_level (= staff or
    instructor) access to the course with the given course_key.
    This ensures the user is authenticated and checks if global staff or has
    staff / instructor access.

    access_level = string, either "staff" or "instructor"
    '''
    if user is None or (not user.is_authenticated()):
        debug("Deny: no user or anon user")
        return False

    if is_masquerading_as_student(user, course_key):
        return False

    if GlobalStaff().has_user(user):
        debug("Allow: user.is_staff")
        return True

    if access_level not in ('staff', 'instructor'):
        log.debug("Error in access._has_access_to_course access_level=%s unknown", access_level)
        debug("Deny: unknown access level")
        return False

    staff_access = (
        CourseStaffRole(course_key).has_user(user) or
        OrgStaffRole(course_key.org).has_user(user)
    )

    if staff_access and access_level == 'staff':
        debug("Allow: user has course staff access")
        return True

    instructor_access = (
        CourseInstructorRole(course_key).has_user(user) or
        OrgInstructorRole(course_key.org).has_user(user)
    )

    if instructor_access and access_level in ('staff', 'instructor'):
        debug("Allow: user has course instructor access")
        return True

    debug("Deny: user did not have correct access")
    return False


def _has_instructor_access_to_descriptor(user, descriptor, course_key):  # pylint: disable=invalid-name
    """Helper method that checks whether the user has staff access to
    the course of the location.

    descriptor: something that has a location attribute
    """
    return _has_instructor_access_to_location(user, descriptor.location, course_key)


def _has_staff_access_to_descriptor(user, descriptor, course_key):
    """Helper method that checks whether the user has staff access to
    the course of the location.

    descriptor: something that has a location attribute
    """
    return _has_staff_access_to_location(user, descriptor.location, course_key)


def is_mobile_available_for_user(user, descriptor):
    """
    Returns whether the given course is mobile_available for the given user.
    Checks:
        mobile_available flag on the course
        Beta User and staff access overrides the mobile_available flag
    Arguments:
        descriptor (CourseDescriptor|CourseOverview): course or overview of course in question
    """
    return (
        descriptor.mobile_available or
        auth.has_access(user, CourseBetaTesterRole(descriptor.id)) or
        _has_staff_access_to_descriptor(user, descriptor, descriptor.id)
    )


def get_user_role(user, course_key):
    """
    Return corresponding string if user has staff, instructor or student
    course role in LMS.
    """
    role = get_masquerade_role(user, course_key)
    if role:
        return role
    elif has_access(user, 'instructor', course_key):
        return 'instructor'
    elif has_access(user, 'staff', course_key):
        return 'staff'
    else:
        return 'student'


def in_preview_mode():
    """
    Returns whether the user is in preview mode or not.
    """
    hostname = get_current_request_hostname()
    return bool(hostname and settings.PREVIEW_DOMAIN in hostname.split('.'))
